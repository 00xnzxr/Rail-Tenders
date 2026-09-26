import { normalizeRole, ROLE_LABELS, ROLE_DESCRIPTIONS } from '@/lib/roles';
import { useState, useEffect } from 'react';
import { Plus, UserCheck, UserX, KeyRound } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  getAdminUsers, createAdminUser, updateAdminUser,
  activateAdminUser, deactivateAdminUser, resetAdminUserPassword,
} from '../../lib/api';
import { formatDateTime } from '../../lib/formatters';

interface AdminUser {
  id: number;
  email: string;
  name: string;
  role: string;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string | null;
}

const ROLE_COLORS: Record<string, string> = {
  master_admin: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  tender_search: 'bg-accent/15 text-accent',
  costing_research: 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  // Legacy values still held by existing users, shown as what they map to.
  admin: 'bg-accent/15 text-accent',
  operator: 'bg-muted text-muted-foreground',
};

export default function UserManagementPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editUser, setEditUser] = useState<AdminUser | null>(null);
  const [form, setForm] = useState({ email: '', name: '', password: '', role: 'costing_research' });
  const [resetPwUser, setResetPwUser] = useState<number | null>(null);
  const [newPassword, setNewPassword] = useState('');

  const fetchUsers = () => {
    setLoading(true);
    getAdminUsers().then(setUsers).finally(() => setLoading(false));
  };

  useEffect(() => { fetchUsers(); }, []);

  const handleCreate = async () => {
    if (!form.email || !form.name || !form.password) return;
    await createAdminUser(form);
    setShowForm(false);
    setForm({ email: '', name: '', password: '', role: 'costing_research' });
    fetchUsers();
  };

  const handleUpdate = async () => {
    if (!editUser) return;
    await updateAdminUser(editUser.id, { name: form.name, email: form.email, role: form.role });
    setEditUser(null);
    fetchUsers();
  };

  const handleToggleActive = async (user: AdminUser) => {
    if (user.is_active) {
      await deactivateAdminUser(user.id);
    } else {
      await activateAdminUser(user.id);
    }
    fetchUsers();
  };

  const handleResetPassword = async () => {
    if (!resetPwUser || !newPassword) return;
    await resetAdminUserPassword(resetPwUser, newPassword);
    setResetPwUser(null);
    setNewPassword('');
  };

  const startEdit = (user: AdminUser) => {
    setEditUser(user);
    setForm({ email: user.email, name: user.name, password: '', role: user.role });
  };

  if (loading) return <><Header title="User Management" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="User Management" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        <div className="flex justify-between items-center">
          <p className="text-sm text-muted-foreground">{users.length} users total</p>
          <button
            onClick={() => { setShowForm(true); setEditUser(null); setForm({ email: '', name: '', password: '', role: 'costing_research' }); }}
            className="flex items-center gap-2 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90"
          >
            <Plus size={16} /> Add User
          </button>
        </div>

        {/* Create/Edit form */}
        {(showForm || editUser) && (
          <div className="bg-card rounded-lg border border-border p-5 space-y-3">
            <h3 className="text-sm font-semibold text-foreground">{editUser ? 'Edit User' : 'Create User'}</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <input type="text" placeholder="Name" value={form.name}
                onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
                className="border border-border rounded-lg px-3 py-2 text-sm" />
              <input type="email" placeholder="Email" value={form.email}
                onChange={(e) => setForm((p) => ({ ...p, email: e.target.value }))}
                className="border border-border rounded-lg px-3 py-2 text-sm" />
              {!editUser && (
                <input type="password" placeholder="Password" value={form.password}
                  onChange={(e) => setForm((p) => ({ ...p, password: e.target.value }))}
                  className="border border-border rounded-lg px-3 py-2 text-sm" />
              )}
              <select value={form.role} onChange={(e) => setForm((p) => ({ ...p, role: e.target.value }))}
                className="border border-border rounded-lg px-3 py-2 text-sm">
                <option value="costing_research">Costing research</option>
                <option value="tender_search">Tender search</option>
                <option value="master_admin">Master admin</option>
              </select>
              <p className="text-xs text-muted-foreground sm:col-span-2">
                {ROLE_DESCRIPTIONS[normalizeRole(form.role)]}
              </p>
            </div>
            <div className="flex gap-2">
              <button onClick={editUser ? handleUpdate : handleCreate}
                className="bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90">
                {editUser ? 'Update' : 'Create'}
              </button>
              <button onClick={() => { setShowForm(false); setEditUser(null); }}
                className="border border-border text-muted-foreground px-4 py-2 rounded-lg text-sm hover:bg-muted/40">
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Reset password form */}
        {resetPwUser && (
          <div className="bg-card rounded-lg border border-orange-200 dark:border-orange-500/20 p-4 flex gap-3 items-end">
            <div className="flex-1">
              <label className="block text-xs text-muted-foreground mb-1">New Password</label>
              <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)}
                className="border border-border rounded-lg px-3 py-2 text-sm w-full" />
            </div>
            <button onClick={handleResetPassword}
              className="bg-orange-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-orange-700">
              Reset
            </button>
            <button onClick={() => { setResetPwUser(null); setNewPassword(''); }}
              className="border border-border text-muted-foreground px-4 py-2 rounded-lg text-sm hover:bg-muted/40">
              Cancel
            </button>
          </div>
        )}

        {/* Users table */}
        <div className="bg-card rounded-lg border border-border overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="bg-muted/40 border-b border-border">
                <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Email</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Role</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Last Login</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground uppercase">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} className="border-b border-border">
                  <td className="px-4 py-3 text-sm font-medium text-foreground">{user.name}</td>
                  <td className="px-4 py-3 text-sm text-muted-foreground">{user.email}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${ROLE_COLORS[normalizeRole(user.role)]}`}>
                      {ROLE_LABELS[normalizeRole(user.role)]}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${user.is_active ? 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400' : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400'}`}>
                      {user.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-foreground">{formatDateTime(user.last_login_at)}</td>
                  <td className="px-4 py-3 text-center">
                    <div className="flex justify-center gap-1">
                      <button onClick={() => startEdit(user)} className="text-xs text-accent hover:underline px-2 py-1">Edit</button>
                      <button onClick={() => handleToggleActive(user)}
                        className={`text-xs px-2 py-1 ${user.is_active ? 'text-red-600 dark:text-red-400 hover:underline' : 'text-emerald-600 dark:text-emerald-400 hover:underline'}`}>
                        {user.is_active ? 'Deactivate' : 'Activate'}
                      </button>
                      <button onClick={() => setResetPwUser(user.id)} className="text-xs text-orange-600 dark:text-orange-400 hover:underline px-2 py-1">
                        Reset Pw
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
