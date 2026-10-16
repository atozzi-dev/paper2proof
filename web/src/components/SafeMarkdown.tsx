import Markdown from 'react-markdown'

// Model-written markdown (the Judge's explanation) is untrusted: raw HTML is dropped, and only
// text formatting survives. Links and images are unwrapped to their text, so nothing loads or
// navigates from model output.
const ALLOWED = ['p', 'em', 'strong', 'code', 'pre', 'ul', 'ol', 'li', 'blockquote', 'br', 'hr', 'del']

export function SafeMarkdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <Markdown skipHtml allowedElements={ALLOWED} unwrapDisallowed>
        {text}
      </Markdown>
    </div>
  )
}
