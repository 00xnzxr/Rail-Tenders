import { SlidersHorizontal, type LucideIcon } from 'lucide-react'
import { Switch } from '@/components/ui/switch'

/**
 * Compact control to flip a page into "Advanced" mode (reveals power-user
 * controls). Pair with the useAdvancedMode hook.
 */
export default function AdvancedToggle({
  checked,
  onCheckedChange,
  label = 'Advanced',
  icon: Icon = SlidersHorizontal,
}: {
  checked: boolean
  onCheckedChange: (v: boolean) => void
  label?: string
  icon?: LucideIcon
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 select-none text-sm text-muted-foreground">
      <Icon size={15} className="shrink-0" />
      <span className="font-medium">{label}</span>
      <Switch
        checked={checked}
        onCheckedChange={onCheckedChange}
        aria-label={`${label} mode`}
      />
    </label>
  )
}
