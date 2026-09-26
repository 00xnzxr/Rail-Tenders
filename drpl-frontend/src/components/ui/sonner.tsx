import { Toaster as Sonner } from 'sonner'

import { useTheme } from '@/context/ThemeContext'

type ToasterProps = React.ComponentProps<typeof Sonner>

/**
 * App toast host. Mount once near the root. Reads the active theme so toasts
 * match light/dark. Use `import { toast } from 'sonner'` to fire toasts.
 */
function Toaster({ ...props }: ToasterProps) {
  const { resolvedTheme } = useTheme()

  return (
    <Sonner
      theme={resolvedTheme as ToasterProps['theme']}
      className="toaster group"
      toastOptions={{
        classNames: {
          toast:
            'group toast group-[.toaster]:bg-card group-[.toaster]:text-card-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg',
          description: 'group-[.toast]:text-muted-foreground',
          actionButton:
            'group-[.toast]:bg-accent group-[.toast]:text-accent-foreground',
          cancelButton:
            'group-[.toast]:bg-muted group-[.toast]:text-muted-foreground',
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
