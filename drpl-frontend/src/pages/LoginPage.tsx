import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { LogIn, Eye, EyeOff, CheckCircle2, Search, FileCheck2, MessageSquare } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';

export default function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await login({ email, password });
      navigate('/');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Login failed. Check your credentials.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="grid min-h-dvh bg-background lg:grid-cols-[minmax(0,1.05fr)_minmax(440px,0.95fr)]">
      <section className="hidden flex-col justify-between overflow-hidden bg-emerald-500 p-10 text-emerald-950 lg:flex xl:p-16">
        <img src="/drpl-logo.svg" alt="DRPL" className="h-11 w-fit" />
        <div className="max-w-xl animate-page-enter">
          <p className="mb-4 text-xs font-extrabold uppercase tracking-[0.18em]">Your daily tender desk</p>
          <h1 className="text-4xl font-extrabold leading-tight tracking-tight xl:text-5xl">Find the right tenders. Prepare them with confidence.</h1>
          <p className="mt-5 max-w-lg text-base font-medium leading-relaxed text-emerald-950/75">DRPL keeps opportunities, checklists, documents, and expert assistance together in one calm workspace.</p>
          <div className="mt-10 grid gap-3 sm:grid-cols-3">
            {[
              { icon: Search, label: 'Find opportunities' },
              { icon: FileCheck2, label: 'Prepare submissions' },
              { icon: MessageSquare, label: 'Ask DRPL anytime' },
            ].map(({ icon: Icon, label }) => (
              <div key={label} className="rounded-2xl bg-white/20 p-4 text-sm font-bold backdrop-blur-sm">
                <Icon size={20} className="mb-3" />{label}
              </div>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2 text-sm font-semibold text-emerald-950/70"><CheckCircle2 size={17} />Simple, guided, and secure</div>
      </section>

      <section className="flex items-center justify-center px-5 py-10 sm:px-10">
      <div className="w-full max-w-md animate-page-enter">
        <div className="mb-8 lg:hidden">
          <img
            src="/drpl-logo.svg"
            alt="DRPL"
            className="mb-3 h-11 w-auto dark:brightness-0 dark:invert"
          />
          <p className="text-sm font-medium text-muted-foreground">Tender Intelligence Platform</p>
        </div>

        <div className="rounded-3xl border border-border bg-card p-7 shadow-card sm:p-9">
          <div className="mb-7">
            <p className="mb-2 text-xs font-extrabold uppercase tracking-[0.16em] text-emerald-700 dark:text-emerald-400">Welcome back</p>
            <h2 className="text-2xl font-extrabold tracking-tight text-foreground">Sign in to your tender desk</h2>
            <p className="mt-2 text-sm text-muted-foreground">Your priorities and unfinished work are waiting on Home.</p>
          </div>

          {error && (
            <div className="mb-5 px-3.5 py-2.5 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive flex items-start gap-2" role="alert">
              <span className="mt-0.5 shrink-0">⚠</span>
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email" className="text-sm font-semibold text-foreground">
                Work email
              </Label>
              <Input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@drpl.com"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-sm font-semibold text-foreground">
                Password
              </Label>
              <div className="relative">
                <Input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  required
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Enter your password"
                  className="pr-10"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                >
                  {showPassword
                    ? <EyeOff size={15} strokeWidth={1.75} />
                    : <Eye size={15} strokeWidth={1.75} />
                  }
                </button>
              </div>
            </div>

            <Button
              type="submit"
              disabled={loading}
              className="mt-2 w-full bg-emerald-600 text-white hover:bg-emerald-700"
            >
              <LogIn size={15} strokeWidth={2} />
              {loading ? 'Opening your desk…' : 'Sign in'}
            </Button>
          </form>
        </div>

        <p className="mt-6 text-center text-xs text-muted-foreground">© {new Date().getFullYear()} DRPL. All rights reserved.</p>
      </div>
      </section>
    </div>
  );
}
