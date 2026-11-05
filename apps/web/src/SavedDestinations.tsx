import { useState } from "react";
import { Json } from "./api";
import { Button, Field, Panel } from "./ui";

export default function SavedDestinations({ rows, id, name, busy, notice, onName, onSelect, onNew, onSave, onRemove }: {
  rows: Json[]; id?: string; name: string; busy: boolean; notice: string;
  onName: (value: string) => void; onSelect: (id: string) => void;
  onNew: () => void; onSave: () => void; onRemove: () => void;
}) {
  const [removing, setRemoving] = useState(false);
  return <Panel title="Saved destinations">
    <p className="saved-destination-help">Save your apps and credentials on this computer for future runs.</p>
    <div className="saved-destination-fields">
      <Field label="Saved destination">
        <select value={id || ""} disabled={busy} onChange={e => { setRemoving(false); onSelect(e.target.value); }}>
          <option value="">Unsaved destination</option>
          {rows.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
        </select>
      </Field>
      <Field label="Destination name">
        <input value={name} maxLength={80} placeholder="e.g. Vantra production" disabled={busy} onChange={e => onName(e.target.value)} />
      </Field>
      <div className="saved-destination-actions">
        <Button disabled={busy} onClick={onSave}>{id ? "Save changes" : "Save destination"}</Button>
        <Button kind="ghost" disabled={busy} onClick={() => { setRemoving(false); onNew(); }}>New destination</Button>
        {id && <Button kind="ghost" disabled={busy} onClick={() => setRemoving(true)}>Remove</Button>}
      </div>
    </div>
    {notice && <p className="saved-destination-notice" role="status">{notice}</p>}
    {removing && id && <div className="saved-destination-remove" role="group" aria-label="Remove saved destination">
      <p>Remove this destination and its saved credentials? Past runs keep their history but cannot reuse these keys.</p>
      <Button disabled={busy} onClick={() => { setRemoving(false); onRemove(); }}>Remove destination and keys</Button>
      <Button kind="ghost" disabled={busy} onClick={() => setRemoving(false)}>Cancel</Button>
    </div>}
  </Panel>;
}
