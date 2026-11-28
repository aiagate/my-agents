import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
export function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none break-words whitespace-pre-wrap prose-headings:mt-4 prose-headings:mb-2 prose-a:text-teal-glow prose-strong:text-ink prose-pre:my-2">
      <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
    </div>
  );
}
