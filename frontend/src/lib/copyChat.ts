/** A selection elsewhere (for example in the file panel) must not be copied from a chat message. */
export function selectedChatText(body: HTMLElement, point?: { x: number; y: number }): string {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return "";
  const range = selection.getRangeAt(0);
  if (!range.intersectsNode(body)) return "";
  // On a file reference, prefer its path menu unless the user right-clicked the actual highlighted text.
  if (point && ![...range.getClientRects()].some((r) =>
    point.x >= r.left && point.x <= r.right && point.y >= r.top && point.y <= r.bottom
  )) return "";
  return selection.toString();
}

export function appendChatQuestion(draft: string, text: string): string {
  const separator = draft && !draft.endsWith("\n") ? "\n" : "";
  return `${draft}${separator}这是什么意思，解释一下：${text}`;
}
