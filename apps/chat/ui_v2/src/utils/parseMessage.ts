export interface ParsedMessage {
  body: string;
  followUps: string[];
}

// Matches a trailing "follow-up questions" style heading so the bullet list under it can be
// lifted out and rendered as interactive chips instead of plain text.
const HEADING_RE =
  /\n\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:follow[-\s]?up(?:\s+questions?)?|you might also ask|you could also ask|related questions?|suggested(?:\s+questions?)?|you may also want to know)\s*[:?]?\s*(?:\*\*|__)?\s*\n/i;

/**
 * Splits an assistant message into its main body and an optional list of follow-up
 * questions. If no follow-up section is detected, the original content is returned as the body.
 */
export function parseMessage(content: string): ParsedMessage {
  if (!content) return { body: content, followUps: [] };

  const match = content.match(HEADING_RE);
  if (!match || match.index === undefined) return { body: content, followUps: [] };

  const before = content.slice(0, match.index);
  const after = content.slice(match.index + match[0].length);
  const lines = after.split('\n');

  const followUps: string[] = [];
  let i = 0;
  while (i < lines.length && lines[i].trim() === '') i++; // skip leading blank lines
  for (; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line === '') break;
    const m = line.match(/^(?:[-*+]|\d+[.)])\s+(.*\S)\s*$/);
    if (!m) break;
    const q = m[1]
      .replace(/^["'\u201c\u201d]+|["'\u201c\u201d]+$/g, '')
      .replace(/\*\*|__|`/g, '')
      .trim();
    if (q) followUps.push(q);
  }

  if (followUps.length === 0) return { body: content, followUps: [] };

  const remainder = lines.slice(i).join('\n').trim();
  const body = (before.trim() + (remainder ? `\n\n${remainder}` : '')).trim();
  return { body, followUps: followUps.slice(0, 6) };
}
