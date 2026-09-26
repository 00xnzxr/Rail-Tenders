import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import Header from '../components/layout/Header';
import AnnexuresStackedView from '../components/workspace/AnnexuresStackedView';

/**
 * Full-page host for the stacked single-tab annexures editor. This is the
 * primary way annexures are filled — replacing the old per-annexure editor
 * pages. All annexures appear stacked as document pages with one-shot export.
 */
export default function AnnexuresWorkspacePage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const tenderId = Number(id);

  return (
    <div className="min-h-screen bg-background">
      <Header title="Prepare annexures" subtitle="Complete the required forms in one place" />
      <main className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="flex items-center gap-3 mb-4">
          <button
            onClick={() => navigate(`/tenders/${tenderId}/command-center`)}
            className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            <ArrowLeft size={16} />
            Ask DRPL
          </button>
        </div>
        <AnnexuresStackedView tenderId={tenderId} />
      </main>
    </div>
  );
}
