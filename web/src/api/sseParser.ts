// Incremental text/event-stream parser (WHATWG "Server-sent events" interpretation rules).

export interface SseFrame {
  id: string | null
  event: string
  data: string
}

export class SseParser {
  private buffer = ''
  private id: string | null = null
  private event = ''
  private data: string[] = []

  /** Feed decoded text; returns every frame completed by it. */
  push(text: string): SseFrame[] {
    this.buffer += text
    const frames: SseFrame[] = []
    let match: RegExpExecArray | null
    const newline = /\r\n|\r|\n/g
    let start = 0
    while ((match = newline.exec(this.buffer)) !== null) {
      // A lone '\r' at the end of the buffer may be the first half of '\r\n': wait for more.
      if (match[0] === '\r' && match.index === this.buffer.length - 1) break
      const line = this.buffer.slice(start, match.index)
      start = match.index + match[0].length
      const frame = this.line(line)
      if (frame) frames.push(frame)
    }
    this.buffer = this.buffer.slice(start)
    return frames
  }

  private line(line: string): SseFrame | null {
    if (line === '') {
      if (this.data.length === 0) {
        this.event = ''
        return null
      }
      const frame = { id: this.id, event: this.event || 'message', data: this.data.join('\n') }
      this.event = ''
      this.data = []
      return frame
    }
    if (line.startsWith(':')) return null // comment (heartbeat)
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'id' && !value.includes('\0')) this.id = value
    else if (field === 'event') this.event = value
    else if (field === 'data') this.data.push(value)
    return null
  }
}
