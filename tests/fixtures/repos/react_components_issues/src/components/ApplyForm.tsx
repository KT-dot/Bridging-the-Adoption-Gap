import React, { useState } from "react";

type Props = { onSubmit: (v: Record<string, string>) => void };

export function ApplyForm({ onSubmit }: Props) {
  const [name, setName] = useState("");
  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit({ name }); }}>
      <input type="text" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
      <label htmlFor="email">Email</label>
      <input id="email" type="email" autoComplete="email" />
      <input type="password" onPaste={(e) => e.preventDefault()} />
      <button type="submit">Submit</button>
    </form>
  );
}
