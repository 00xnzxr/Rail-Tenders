const workflowConfig: Record<string, { label: string; className: string }> = {
  new: { label: 'New', className: 'text-muted-foreground bg-muted' },
  in_progress: { label: 'In Progress', className: 'text-blue-700 bg-blue-100 dark:bg-blue-500/15 dark:text-blue-400' },
  checklist_ready: { label: 'Checklist Ready', className: 'text-indigo-700 bg-indigo-100 dark:bg-indigo-500/15 dark:text-indigo-400' },
  proposal_draft: { label: 'Proposal Draft', className: 'text-purple-700 bg-purple-100 dark:bg-purple-500/15 dark:text-purple-400' },
  proposal_review: { label: 'Under Review', className: 'text-yellow-700 bg-yellow-100 dark:bg-yellow-500/15 dark:text-yellow-400' },
  approved: { label: 'Approved', className: 'text-emerald-700 bg-emerald-100 dark:bg-emerald-500/15 dark:text-emerald-400' },
  submitted: { label: 'Submitted', className: 'text-teal-700 bg-teal-100 dark:bg-teal-500/15 dark:text-teal-400' },
};

export default function WorkflowBadge({ status }: { status: string }) {
  const config = workflowConfig[status] || workflowConfig.new;
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium whitespace-nowrap ${config.className}`}>
      {config.label}
    </span>
  );
}
