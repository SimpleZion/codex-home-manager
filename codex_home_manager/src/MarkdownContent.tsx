import React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";
import type { Element, Parent, Root, RootContent, Text } from "hast";
import type { PluggableList, Plugin } from "unified";
import { findSearchMatches } from "./threadContentSearch";

type Props = { text: string; searchQuery: string; ariaLabel: string; onRendered: () => void };
type SearchPluginOptions = { query: string };

const rehypeMarkSearch: Plugin<[SearchPluginOptions], Root> = ({ query }) => {
  return (tree) => {
    visit(tree, "text", (node: Text, index, parent: Parent | undefined) => {
      if (typeof index !== "number" || !parent || typeof node.value !== "string") return;
      const parentTag = "tagName" in parent ? String((parent as Element).tagName) : "";
      if (["script", "style"].includes(parentTag)) return;
      const matches = findSearchMatches(node.value, query);
      if (!matches.length) return;
      const children: RootContent[] = [];
      let cursor = 0;
      for (const match of matches) {
        if (match.start > cursor) children.push({ type: "text", value: node.value.slice(cursor, match.start) });
        children.push({ type: "element", tagName: "mark", properties: {}, children: [{ type: "text", value: node.value.slice(match.start, match.end) }] });
        cursor = match.end;
      }
      if (cursor < node.value.length) children.push({ type: "text", value: node.value.slice(cursor) });
      parent.children.splice(index, 1, ...children);
      return index + children.length;
    });
  };
}

const markdownComponents: Components = {
  a({ node: _node, ...props }) { return <a {...props} target="_blank" rel="noreferrer noopener" />; },
  table({ node: _node, ...props }) { return <div className="timeline-markdown-table-wrap" tabIndex={0}><table {...props} /></div>; },
  img({ node: _node, src, alt }) { return <span className="timeline-markdown-image" title={typeof src === "string" ? src : undefined}>[{alt || "图片"}]</span>; }
};

export default function MarkdownContent({ text, searchQuery, ariaLabel, onRendered }: Props) {
  const rehypePlugins = React.useMemo<PluggableList>(() => searchQuery ? [[rehypeMarkSearch, { query: searchQuery }]] : [], [searchQuery]);
  React.useLayoutEffect(() => {
    onRendered();
    const frame = window.requestAnimationFrame(onRendered);
    return () => window.cancelAnimationFrame(frame);
  }, [onRendered, searchQuery, text]);
  return <div className="timeline-readable-text timeline-markdown" tabIndex={0} aria-label={ariaLabel}><ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={rehypePlugins} skipHtml components={markdownComponents}>{text}</ReactMarkdown></div>;
}
