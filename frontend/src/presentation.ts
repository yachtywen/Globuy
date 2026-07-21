const HIDDEN_RESULT_PATTERNS = [
  /运费/i,
  /邮费/i,
  /包邮/i,
  /shipping[_ ]?fee/i,
  /离线快照/i,
  /离线数据快照/i,
  /数据说明/i,
];

export function isHiddenResultText(value: string) {
  return HIDDEN_RESULT_PATTERNS.some((pattern) => pattern.test(value));
}

export function visibleResultStrings(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && Boolean(item) && !isHiddenResultText(item))
    : [];
}

export function sanitizeShoppingMarkdown(value: string) {
  return value
    .split(/\r?\n/)
    .filter((line) => !isHiddenResultText(line))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
