import { normalizeRole, MASTER_ADMIN, TENDER_SEARCH } from '@/lib/roles';
import { createContext, useContext, useState, useCallback, useEffect, useMemo, type ReactNode } from 'react';
import { login as apiLogin, getCurrentUser } from '../lib/api';
import type { AuthState, LoginCredentials, UserRole } from '../types/auth';

interface AuthContextValue {
  auth: AuthState;
  login: (credentials: LoginCredentials) => Promise<void>;
  logout: () => void;
  isMasterAdmin: boolean;
  isAdminOrAbove: boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function getInitialState(): AuthState {
  const token = localStorage.getItem('drpl_token');
  const email = localStorage.getItem('drpl_email');
  const userId = localStorage.getItem('drpl_user_id');
  const role = localStorage.getItem('drpl_role');
  return {
    token,
    email,
    userId: userId ? Number(userId) : null,
    role,
    isAuthenticated: !!token,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState>(getInitialState);

  useEffect(() => {
    if (auth.isAuthenticated && !auth.role) {
      getCurrentUser()
        .then((profile) => {
          localStorage.setItem('drpl_role', profile.role);
          setAuth((prev) => ({ ...prev, role: profile.role }));
        })
        .catch(() => {});
    }
  }, [auth.isAuthenticated, auth.role]);

  const login = useCallback(async (credentials: LoginCredentials) => {
    const response = await apiLogin(credentials);
    localStorage.setItem('drpl_token', response.access_token);
    localStorage.setItem('drpl_email', response.email);
    localStorage.setItem('drpl_user_id', String(response.user_id));

    try {
      const profile = await getCurrentUser();
      localStorage.setItem('drpl_role', profile.role);
      setAuth({
        token: response.access_token,
        email: response.email,
        userId: response.user_id,
        role: profile.role,
        isAuthenticated: true,
      });
    } catch {
      setAuth({
        token: response.access_token,
        email: response.email,
        userId: response.user_id,
        role: null,
        isAuthenticated: true,
      });
    }
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem('drpl_token');
    localStorage.removeItem('drpl_email');
    localStorage.removeItem('drpl_user_id');
    localStorage.removeItem('drpl_role');
    setAuth({ token: null, email: null, userId: null, role: null, isAuthenticated: false });
  }, []);

  // Normalized so the legacy values users hold today ('admin', 'operator')
  // keep behaving as they did, and the new role names work alongside them.
  const normalized = normalizeRole(auth.role);
  const isMasterAdmin = normalized === MASTER_ADMIN;
  const isAdminOrAbove = normalized === MASTER_ADMIN || normalized === TENDER_SEARCH;

  const value = useMemo(() => ({
    auth, login, logout, isMasterAdmin, isAdminOrAbove,
  }), [auth, login, logout, isMasterAdmin, isAdminOrAbove]);

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within AuthProvider');
  return context;
}
