// XSS-safe markdown rendering for note previews (mirrors the desktop renderer's config).
//
// html:false escapes any raw HTML in the source instead of emitting tags; validateLink
// blocks javascript:/data:/vbscript: URLs; the default renderer always HTML-escapes
// attribute values. This is the same protection the desktop chat uses.
import MarkdownIt from "markdown-it";

function isSafeLink(url: string): boolean {
  const u = String(url || "").trim().toLowerCase();
  if (!u) return false;
  if (u.startsWith("http://") || u.startsWith("https://") || u.startsWith("mailto:")) return true;
  if (u.startsWith("/") || u.startsWith("./") || u.startsWith("../") || u.startsWith("#")) return true;
  return !/^[a-z][a-z0-9+.-]*:/i.test(u); // no scheme → bare relative reference
}

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true, // soft line breaks → <br>, matches the desktop chat rendering
});
// markdown-it v15 types declare validateLink as an instance method, not an option.
md.validateLink = isSafeLink;

export function renderMarkdown(text: string): string {
  return md.render(String(text ?? ""));
}
