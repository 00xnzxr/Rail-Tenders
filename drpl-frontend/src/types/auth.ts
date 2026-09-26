export type UserRole = 'operator' | 'admin' | 'master_admin';

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface AuthToken {
  access_token: string;
  token_type: string;
  user_id: number;
  email: string;
}

export interface AuthState {
  token: string | null;
  email: string | null;
  userId: number | null;
  role: string | null;
  isAuthenticated: boolean;
}

export interface UserProfile {
  id: number;
  email: string;
  name: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

// API Tokens
export interface CreateAPITokenResponse {
  id: number;
  name: string;
  token: string;
  prefix: string;
  created_at: string;
}

export interface APITokenInfo {
  id: number;
  name: string;
  prefix: string;
  last_used_at: string | null;
  is_active: boolean;
  created_at: string;
}

// Extension Status
export interface ExtensionStatus {
  connected: boolean;
  last_sync: string | null;
  active_portals: string[];
  tenders_uploaded_24h: number;
}
