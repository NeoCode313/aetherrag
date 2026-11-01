import type {Citation} from '../types/api';

declare global {
  interface Window {
    marked?: {parse: (value: string, options?: {gfm: boolean; breaks: boolean; async: false}) => string};
    DOMPurify?: {sanitize: (value: string, options: object) => DocumentFragment};
  }
}
export function safeMarkdown(answer: string, citations: Citation[]): string {
  const wrapper = document.createElement('div');
  if (!window.marked || !window.DOMPurify) {wrapper.textContent = answer; return wrapper.innerHTML;}
  wrapper.append(window.DOMPurify.sanitize(window.marked.parse(answer, {gfm:true, breaks:true, async:false}), {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS:['p','br','strong','em','del','ul','ol','li','blockquote','pre','code','h1','h2','h3','h4','table','thead','tbody','tr','th','td','hr','a'],
    ALLOWED_ATTR:['href','title','colspan','rowspan'], ALLOW_DATA_ATTR:false,
    FORBID_TAGS:['img','svg','script','style','iframe','object','embed','form','input','button'],
    FORBID_ATTR:['onerror','onload','onclick','onmouseover','onfocus','style'],
  }));
  wrapper.querySelectorAll('a').forEach(anchor => {
    const href = anchor.getAttribute('href') || '';
    if (href.startsWith('#')) return;
    try {
      const url = new URL(href);
      if (!['https:','http:'].includes(url.protocol)) anchor.removeAttribute('href');
      else {anchor.rel = 'noopener noreferrer'; anchor.target = '_blank';}
    } catch {anchor.removeAttribute('href');}
  });
  const refs = new Set(citations.map(citation => citation.ref));
  const walker = document.createTreeWalker(wrapper, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  while (walker.nextNode()) texts.push(walker.currentNode as Text);
  for (const text of texts) {
    if (text.parentElement?.closest('code,pre,a')) continue;
    const matcher = /\[(\d+)\]/g, fragment = document.createDocumentFragment();
    let start = 0, match: RegExpExecArray | null;
    while ((match = matcher.exec(text.data))) {
      fragment.append(document.createTextNode(text.data.slice(start, match.index)));
      const ref = Number(match[1]);
      if (refs.has(ref)) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'inline-cite-btn';
        button.dataset.ref = String(ref); button.textContent = '[' + ref + ']'; fragment.append(button);
      } else fragment.append(document.createTextNode(match[0]));
      start = matcher.lastIndex;
    }
    if (start) {fragment.append(document.createTextNode(text.data.slice(start))); text.replaceWith(fragment);}
  }
  return wrapper.innerHTML;
}
export function safeImageUrl(value?: string): string | undefined {
  if (!value) return;
  try {
    const url = new URL(value, location.origin);
    if (url.origin === location.origin && /^\/api\/v1\/(assets|pages)\//.test(url.pathname)) return url.href;
  } catch { /* Invalid or foreign image URLs are not loaded. */ }
}
export const numberText = (value: unknown, digits = 3) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : 'N/A';
export const boxText = (value?: number[]) => value ? '[' + value.map(n => numberText(n)).join(', ') + ']' : 'N/A';
