import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircle, XCircle, MessageSquare } from 'lucide-react';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import PortalBadge from '../components/ui/PortalBadge';
import { getPendingReviews, approveReview, rejectReview, requestReviewChanges } from '../lib/api';
import { formatDateTime } from '../lib/formatters';
import type { PendingReview } from '../types/tender';

export default function AdminReviewsPage() {
  const navigate = useNavigate();
  const [reviews, setReviews] = useState<PendingReview[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionReviewId, setActionReviewId] = useState<number | null>(null);
  const [comments, setComments] = useState('');
  const [actionType, setActionType] = useState<'approve' | 'reject' | 'changes' | null>(null);

  const fetchReviews = () => {
    setLoading(true);
    getPendingReviews()
      .then(setReviews)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchReviews(); }, []);

  const handleAction = async () => {
    if (!actionReviewId || !actionType) return;
    try {
      if (actionType === 'approve') await approveReview(actionReviewId, comments);
      else if (actionType === 'reject') await rejectReview(actionReviewId, comments);
      else if (actionType === 'changes') await requestReviewChanges(actionReviewId, comments);
      setActionReviewId(null);
      setComments('');
      setActionType(null);
      fetchReviews();
    } catch {}
  };

  return (
    <>
      <Header title="Proposal reviews" subtitle="Approve, return, or reject submitted proposals" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {loading ? (
          <LoadingSpinner />
        ) : reviews.length === 0 ? (
          <div className="rounded-2xl border border-border bg-card p-12 text-center shadow-card">
            <CheckCircle size={40} className="mx-auto text-muted-foreground/50 mb-3" />
            <p className="text-sm text-muted-foreground">No pending reviews</p>
          </div>
        ) : (
          <div className="space-y-4">
            {reviews.map((review) => (
              <div key={review.id} className="rounded-2xl border border-border bg-card p-6 shadow-card">
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="font-semibold text-foreground">{review.tender_title}</h3>
                    <div className="flex items-center gap-2 mt-1">
                      {review.tender_portal && <PortalBadge portal={review.tender_portal} />}
                      <span className="text-xs text-muted-foreground">
                        Submitted {formatDateTime(review.created_at)}
                      </span>
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => review.tender_id && navigate(`/tenders/${review.tender_id}/command-center`)}
                      className="text-sm text-accent hover:underline"
                    >
                      View Proposal
                    </button>
                  </div>
                </div>

                {actionReviewId === review.id ? (
                  <div className="mt-4 space-y-3">
                    <textarea
                      value={comments}
                      onChange={(e) => setComments(e.target.value)}
                      placeholder="Add comments (optional)..."
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                      rows={3}
                    />
                    <div className="flex gap-2">
                      <button
                        onClick={handleAction}
                        className={`px-4 py-2 rounded-lg text-sm font-medium text-white ${
                          actionType === 'approve' ? 'bg-emerald-600 hover:bg-emerald-700' :
                          actionType === 'reject' ? 'bg-red-600 hover:bg-red-700' :
                          'bg-orange-600 hover:bg-orange-700'
                        }`}
                      >
                        Confirm {actionType === 'approve' ? 'Approval' : actionType === 'reject' ? 'Rejection' : 'Request Changes'}
                      </button>
                      <button
                        onClick={() => { setActionReviewId(null); setActionType(null); setComments(''); }}
                        className="px-4 py-2 rounded-lg text-sm font-medium border border-border text-muted-foreground hover:bg-muted/40"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="mt-4 flex gap-2">
                    <button
                      onClick={() => { setActionReviewId(review.id); setActionType('approve'); }}
                      className="flex items-center gap-1 bg-emerald-600 text-white px-3 py-1.5 rounded-lg text-sm font-medium hover:bg-emerald-700"
                    >
                      <CheckCircle size={14} /> Approve
                    </button>
                    <button
                      onClick={() => { setActionReviewId(review.id); setActionType('changes'); }}
                      className="flex items-center gap-1 bg-orange-600 text-white px-3 py-1.5 rounded-lg text-sm font-medium hover:bg-orange-700"
                    >
                      <MessageSquare size={14} /> Request Changes
                    </button>
                    <button
                      onClick={() => { setActionReviewId(review.id); setActionType('reject'); }}
                      className="flex items-center gap-1 bg-red-600 text-white px-3 py-1.5 rounded-lg text-sm font-medium hover:bg-red-700"
                    >
                      <XCircle size={14} /> Reject
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
