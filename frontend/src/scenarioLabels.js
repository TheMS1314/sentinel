// Raw scenario_tag values look like "well_behaved" or
// "adversarial:merchant_swap" -- accurate for logs/filtering, but not
// something a viewer watching a demo recording should have to parse.
// This is presentation-only: the raw tag is still what's sent to the
// API for filtering, this just controls what's rendered.

const KNOWN_LABELS = {
  well_behaved: "Well-behaved agent",
  "adversarial:amount_creep": "Amount creep attack",
  "adversarial:category_drift": "Category drift attack",
  "adversarial:velocity_spike": "Velocity spike attack",
  "adversarial:off_hours": "Off-hours attempt",
  "adversarial:prompt_injection": "Prompt injection attack",
  "adversarial:merchant_swap": "Merchant swap attack",
  ap2: "AP2 mandate",
};

function titleCaseFallback(tag) {
  // Unknown tag shape -- best-effort humanization rather than a blank
  // or a raw snake_case/colon string: strip the "adversarial:" prefix
  // into a suffix note, and turn underscores into spaces.
  const [prefix, rest] = tag.includes(":") ? tag.split(":", 2) : [null, tag];
  const words = rest.replace(/_/g, " ").trim();
  const label = words.charAt(0).toUpperCase() + words.slice(1);
  return prefix ? `${label} (${prefix})` : label;
}

export function humanizeScenarioTag(tag) {
  if (!tag) return "Untagged";
  return KNOWN_LABELS[tag] || titleCaseFallback(tag);
}
