import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';
import clsx from 'clsx';

interface PaginationProps {
  offset: number;
  limit: number;
  total: number;
  onChange: (newOffset: number) => void;
}

export default function Pagination({ offset, limit, total, onChange }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.floor(offset / limit) + 1;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);

  // windowed page numbers around current
  const pages: number[] = [];
  const start = Math.max(1, currentPage - 2);
  const end = Math.min(totalPages, start + 4);
  for (let p = start; p <= end; p++) pages.push(p);

  const go = (page: number) => onChange((page - 1) * limit);
  const btn = 'flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors';
  const disabled = 'border-border text-muted-foreground/50 cursor-not-allowed';
  const active = 'border-border text-foreground hover:bg-muted';

  return (
    <div className="flex items-center justify-between pt-4 flex-wrap gap-2">
      <p className="text-sm text-muted-foreground">
        Showing {from}-{to} of {total.toLocaleString()} · Page {currentPage} of {totalPages}
      </p>
      <div className="flex items-center gap-1">
        <button onClick={() => go(1)} disabled={currentPage === 1} className={clsx(btn, currentPage === 1 ? disabled : active)} aria-label="First page">
          <ChevronsLeft size={16} />
        </button>
        <button onClick={() => go(currentPage - 1)} disabled={currentPage === 1} className={clsx(btn, currentPage === 1 ? disabled : active)}>
          <ChevronLeft size={16} /> Prev
        </button>
        {pages.map((p) => (
          <button key={p} onClick={() => go(p)}
            className={clsx('px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors',
              p === currentPage ? 'bg-accent text-accent-foreground border-accent' : active)}>
            {p}
          </button>
        ))}
        <button onClick={() => go(currentPage + 1)} disabled={currentPage >= totalPages} className={clsx(btn, currentPage >= totalPages ? disabled : active)}>
          Next <ChevronRight size={16} />
        </button>
        <button onClick={() => go(totalPages)} disabled={currentPage >= totalPages} className={clsx(btn, currentPage >= totalPages ? disabled : active)} aria-label="Last page">
          <ChevronsRight size={16} />
        </button>
      </div>
    </div>
  );
}
