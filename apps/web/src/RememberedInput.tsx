import { ReactNode, useEffect, useId, useRef, useState } from "react";
import { ChevronDownIcon, CheckIcon } from "@radix-ui/react-icons";

export type Suggestion = { id: string; label: string; detail?: string; selected?: boolean };

/** Saved values stay in the existing field; secrets are represented only by opaque IDs and masks. */
export default function RememberedInput({ label, value, onChange, onSelect, options, hint, placeholder,
  secret = false, maxLength, onBlur }: {
  label: string; value: string; onChange: (value: string) => void; onSelect: (id: string) => void;
  options: Suggestion[]; hint?: ReactNode; placeholder?: string; secret?: boolean;
  maxLength?: number; onBlur?: () => void;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [active, setActive] = useState(-1);
  const visible = options.filter(o => secret || (o.label + " " + (o.detail || "")).toLowerCase().includes(filter.toLowerCase()));
  const expanded = open && visible.length > 0;
  useEffect(() => { list.current?.children[active]?.scrollIntoView({block: "nearest"}); }, [active]);
  const choose = (option: Suggestion) => { onSelect(option.id); setOpen(false); setActive(-1); };
  return <div className="field remembered-field" onBlur={e => {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) { setOpen(false); setActive(-1); onBlur?.(); }
  }}>
    <label htmlFor={id}>{label}</label>
    <div className="remembered-control">
      <input id={id} ref={input} role="combobox" type={secret ? "password" : "text"}
        aria-autocomplete="list" aria-expanded={expanded} aria-controls={expanded ? id + "-list" : undefined}
        aria-activedescendant={expanded && active >= 0 && visible[active] ? id + "-option-" + active : undefined}
        aria-describedby={hint ? id + "-hint" : undefined} autoComplete={secret ? "new-password" : "off"}
        className={options.length ? "has-suggestions" : ""} value={value} placeholder={placeholder} maxLength={maxLength}
        onFocus={() => { setFilter(""); setActive(-1); setOpen(true); }}
        onChange={e => { onChange(e.target.value); setFilter(secret ? "" : e.target.value); setActive(-1); setOpen(true); }}
        onKeyDown={e => {
          if (e.key === "Escape") { e.preventDefault(); setOpen(false); setActive(-1); }
          if ((e.key === "ArrowDown" || e.key === "ArrowUp") && visible.length) {
            e.preventDefault(); setOpen(true);
            setActive(i => i < 0 ? (e.key === "ArrowDown" ? 0 : visible.length - 1)
              : (i + (e.key === "ArrowDown" ? 1 : -1) + visible.length) % visible.length);
          }
          if (e.key === "Enter" && expanded && active >= 0 && visible[active]) { e.preventDefault(); choose(visible[active]); }
        }} />
      {!!options.length && <button type="button" className="remembered-toggle" aria-label={"Show saved " + label}
        aria-expanded={expanded} onClick={() => { const next = !expanded; input.current?.focus(); setFilter(""); setActive(-1); setOpen(next); }}>
        <ChevronDownIcon aria-hidden="true" />
      </button>}
      {expanded && <div ref={list} id={id + "-list"} role="listbox" aria-label={"Saved " + label} className="remembered-options">
        {visible.map((option, i) => <button type="button" role="option" tabIndex={-1} id={id + "-option-" + i}
          key={option.id} aria-selected={!!option.selected} className={i === active ? "active" : ""}
          onMouseDown={e => e.preventDefault()} onClick={() => choose(option)}>
          <span><strong>{option.label}</strong>{option.detail && <small>{option.detail}</small>}</span>
          {option.selected && <CheckIcon aria-hidden="true" />}
        </button>)}
      </div>}
    </div>
    {hint && <small id={id + "-hint"}>{hint}</small>}
  </div>;
}
