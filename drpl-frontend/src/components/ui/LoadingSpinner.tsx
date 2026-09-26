import { Loader2 } from 'lucide-react';

export default function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-20">
      <Loader2 className="animate-spin text-accent" size={32} />
    </div>
  );
}
