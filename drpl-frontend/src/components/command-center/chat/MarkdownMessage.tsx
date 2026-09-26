import { useState, memo, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
// Async-light Prism: language grammars load on demand instead of bundling all
// of them up front — keeps the main chunk small.
import SyntaxHighlighter from 'react-syntax-highlighter/dist/esm/prism-async-light'
import { oneLight, oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { Check, Copy } from 'lucide-react'

import { useTheme } from '@/context/ThemeContext'
import { cn } from '@/lib/utils'

function CopyButton({ getText }: { getText: () => string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      aria-label="Copy code"
      onClick={() => {
        navigator.clipboard.writeText(getText()).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        })
      }}
      className="inline-flex items-center gap-1 rounded-md border border-border bg-card/80 px-2 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

/**
 * Shared markdown renderer for chat. Adds:
 *  - syntax-highlighted code blocks with a copy button (theme-aware)
 *  - scrollable wide tables
 *  - token-driven prose that works in light + dark (`prose dark:prose-invert`)
 */
function MarkdownMessageImpl({
  content,
  variant = 'default',
}: {
  content: string
  variant?: 'default' | 'compact'
}) {
  const { resolvedTheme } = useTheme()
  const codeStyle = resolvedTheme === 'dark' ? oneDark : oneLight

  return (
    <div
      className={cn(
        'prose prose-sm max-w-none break-words dark:prose-invert prose-pre:bg-transparent prose-pre:p-0',
        variant === 'compact' && [
          'text-[13px] leading-5',
          'prose-headings:font-semibold prose-headings:leading-snug prose-headings:text-foreground',
          'prose-h1:mb-2 prose-h1:mt-1 prose-h1:text-lg',
          'prose-h2:mb-2 prose-h2:mt-3 prose-h2:text-base',
          'prose-h3:mb-1.5 prose-h3:mt-3 prose-h3:text-sm',
          'prose-h4:mb-1 prose-h4:mt-2 prose-h4:text-sm',
          'prose-p:my-2 prose-p:leading-5',
          'prose-ul:my-2 prose-ul:pl-5 prose-ol:my-2 prose-ol:pl-5',
          'prose-li:my-0.5 prose-li:leading-5',
          'prose-strong:font-semibold',
          'prose-blockquote:my-2 prose-blockquote:border-l-2 prose-blockquote:pl-3',
          'prose-hr:my-3',
        ],
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children, ...props }: any) => (
            <div className="prose-table-wrap">
              <table {...props}>{children}</table>
            </div>
          ),
          code({ className, children, ...props }: any) {
            // react-markdown v10 removed the `inline` prop. Detect block code
            // by a language class or a multi-line body; everything else is an
            // inline code span.
            const match = /language-(\w+)/.exec(className || '')
            const raw = String(children).replace(/\n$/, '')
            const isBlock = !!match || raw.includes('\n')
            if (!isBlock) {
              return (
                <code
                  className={cn(
                    'rounded bg-muted px-1.5 py-0.5 text-[0.85em] font-medium text-foreground',
                    className
                  )}
                  {...props}
                >
                  {children}
                </code>
              )
            }
            const lang = match?.[1] ?? 'text'
            return (
              <div className="group/code relative my-3 overflow-hidden rounded-lg border border-border">
                <div className="flex items-center justify-between border-b border-border bg-muted/50 px-3 py-1.5">
                  <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    {lang}
                  </span>
                  <CopyButton getText={() => raw} />
                </div>
                <SyntaxHighlighter
                  language={lang}
                  style={codeStyle}
                  customStyle={{
                    margin: 0,
                    background: 'transparent',
                    fontSize: '0.8rem',
                    padding: '0.85rem 1rem',
                  }}
                  PreTag="div"
                >
                  {raw}
                </SyntaxHighlighter>
              </div>
            )
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

export const MarkdownMessage = memo(MarkdownMessageImpl)

/**
 * Inline collapsible long content — replaces the old hard 700-char cut.
 * Renders the full message but visually clamps very long ones with a soft
 * gradient + "Show more" toggle (ChatGPT/Claude behaviour).
 */
const COLLAPSE_THRESHOLD = 1400

export function CollapsibleMarkdown({
  content,
  footer,
  variant = 'default',
}: {
  content: string
  footer?: ReactNode
  variant?: 'default' | 'compact'
}) {
  const [expanded, setExpanded] = useState(false)
  const isLong = content.length > COLLAPSE_THRESHOLD

  return (
    <div>
      <div className={cn('relative', isLong && !expanded && 'max-h-[22rem] overflow-hidden')}>
        <MarkdownMessage content={content} variant={variant} />
        {/* Fade uses `from-background` — correct only when rendered on the page
            background (assistant messages). If ever placed inside a card/bubble
            with a different background, change to from-card / from-muted. */}
        {isLong && !expanded && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-background to-transparent" />
        )}
      </div>
      {isLong && (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="mt-2 text-xs font-medium text-accent hover:underline"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
      {footer}
    </div>
  )
}
