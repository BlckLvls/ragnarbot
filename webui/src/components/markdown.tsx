import { memo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

function CodeBlock({ className, children }: { className?: string; children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false)
  const lang = /language-(\w+)/.exec(className || '')?.[1] ?? ''
  const text = String(children ?? '').replace(/\n$/, '')
  const copy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div className="my-2 overflow-hidden rounded-[4px] bg-deep">
      <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
        <span className="font-mono text-[9.5px] uppercase tracking-wider text-faint">{lang || 'code'}</span>
        <button onClick={copy} className="font-mono text-[9.5px] text-acc hover:opacity-80">
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <pre className="overflow-x-auto p-3">
        <code className="bg-transparent p-0 font-mono text-[11px] leading-[1.6] text-mist">{text}</code>
      </pre>
    </div>
  )
}

export const Markdown = memo(function Markdown({
  children,
  streaming = false,
}: {
  children: string
  streaming?: boolean
}) {
  return (
    <div className={`rb-md ${streaming ? 'rb-md-streaming' : ''}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // The console is an SPA — external links must not replace the app.
          a: ({ href, children, ...props }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
              {children}
            </a>
          ),
          pre: ({ children }) => <>{children}</>,
          code: ({ className, children, ...props }) => {
            const inline = !className && !String(children).includes('\n')
            if (inline) {
              return (
                <code className="rounded-[2px] bg-raised2 px-[5px] py-[1px] font-mono text-[11.5px] text-mist" {...props}>
                  {children}
                </code>
              )
            }
            return <CodeBlock className={className}>{children}</CodeBlock>
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
})
