import Markdown from 'react-markdown'

// Model-written markdown (the Judge's explanation) is untrusted: raw HTML is dropped, and only
// text formatting survives. Links and images are unwrapped to their text, so nothing loads or
// navigates from model output.
const ALLOWED = ['p', 'em', 'strong', 'code', 'pre', 'ul', 'ol', 'li', 'blockquote', 'br', 'hr', 'del']

// Code spans and fenced blocks are left exactly as written.
const CODE = /(```[\s\S]*?```|`[^`\n]*`)/
// A code name: letters or digits joined by underscores (load_breast_cancer, __init__, _x_).
// An underscore the model already escaped (\_) is left as it is.
const NAME = /(?<![\\A-Za-z0-9_])(?:[A-Za-z0-9]|_(?<!\\_))+/g

/** Underscores in code names are literal, never emphasis: "__init__" would otherwise read
 * "init" in bold and "_load_breast_cancer_" lose its outer underscores. Emphasis with * stays. */
function keepNames(text: string): string {
  return text
    .split(CODE)
    .map((part, i) => (i % 2 === 1 ? part : part.replace(NAME, (name) => (/[A-Za-z0-9]/.test(name) ? name.replace(/_/g, '\\_') : name))))
    .join('')
}

export function SafeMarkdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <Markdown skipHtml allowedElements={ALLOWED} unwrapDisallowed>
        {keepNames(text)}
      </Markdown>
    </div>
  )
}
