import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * Merge Tailwind class names — combines clsx (conditional classes) with
 * tailwind-merge (dedupes conflicting Tailwind utilities, last wins).
 * The standard shadcn/ui helper used by every primitive.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
