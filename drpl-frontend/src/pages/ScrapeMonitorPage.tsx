import Header from '../components/layout/Header';
import ScrapeLogTable from '../components/scrape/ScrapeLogTable';
import PortalHealthCard from '../components/monitoring/PortalHealthCard';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import { useScrapeLog } from '../hooks/useScrapeLog';
import { usePortalHealth } from '../hooks/usePortalHealth';
import { portalLabel } from '../lib/formatters';
import { RefreshCw } from 'lucide-react';

export default function ScrapeMonitorPage() {
  const { logs, loading } = useScrapeLog();
  const { health, alerts, loading: healthLoading } = usePortalHealth();

  return (
    <>
      <Header title="Scrape Monitor" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">
        {/* Portal Health — scrape internals per portal (all portals shown here). */}
        <section>
          <h3 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-widest mb-4">Portal Health</h3>
          {healthLoading ? (
            <LoadingSpinner />
          ) : (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                {health.map((h) => (
                  <PortalHealthCard key={h.portal} health={h} />
                ))}
              </div>
              {alerts.length > 0 && (
                <div className="mt-3 space-y-2">
                  {alerts.map((alert, i) => (
                    <div
                      key={i}
                      className={`px-4 py-2.5 rounded-lg text-sm flex items-center gap-2 ${
                        alert.severity === 'critical'
                          ? 'bg-red-50 border border-red-100 text-red-700 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400'
                          : 'bg-amber-50 border border-amber-100 text-amber-700 dark:bg-amber-500/15 dark:border-amber-500/20 dark:text-amber-400'
                      }`}
                    >
                      <span className="font-semibold">{portalLabel(alert.portal)}:</span>
                      {alert.message}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </section>

        {/* Scrape sessions */}
        <section className="space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              Extension scraping sessions are shown below. Auto-refreshes every 10 seconds.
            </p>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <RefreshCw size={12} className="animate-spin" />
              Live
            </div>
          </div>

          {loading ? <LoadingSpinner /> : <ScrapeLogTable logs={logs} />}
        </section>
      </div>
    </>
  );
}
