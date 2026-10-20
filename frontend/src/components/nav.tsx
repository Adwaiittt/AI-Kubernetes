"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useState } from "react";

import { setApproverKey, useApproverKey } from "@/lib/approver-key";

import { Button } from "./ui";

const LINKS = [
  { href: "/", label: "Incidents" },
  { href: "/incidents/new", label: "New investigation" },
  { href: "/audit", label: "Audit log" },
];

export function Nav() {
  return (
    <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" className="flex items-center gap-2 font-semibold">
          <span className="grid size-6 place-items-center rounded bg-slate-900 text-xs text-white dark:bg-slate-100 dark:text-slate-900">
            K
          </span>
          AI SRE Monitor
        </Link>
        <nav className="flex gap-1 text-sm">
          {/* Highlighting the active link needs the pathname, which streams in after prerender. */}
          <Suspense fallback={<NavLinks pathname={null} />}>
            <ActiveNavLinks />
          </Suspense>
        </nav>
        <div className="ml-auto">
          <ApproverKey />
        </div>
      </div>
    </header>
  );
}

function ActiveNavLinks() {
  return <NavLinks pathname={usePathname()} />;
}

function NavLinks({ pathname }: { pathname: string | null }) {
  return LINKS.map((l) => {
    const active =
      pathname !== null && (l.href === "/" ? pathname === "/" : pathname.startsWith(l.href));
    return (
      <Link
        key={l.href}
        href={l.href}
        className={`rounded-md px-2.5 py-1.5 ${
          active
            ? "bg-slate-100 font-medium dark:bg-slate-800"
            : "text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
        }`}
      >
        {l.label}
      </Link>
    );
  });
}

function ApproverKey() {
  const key = useApproverKey();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");

  if (editing) {
    return (
      <form
        className="flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setApproverKey(value.trim() || null);
          setValue("");
          setEditing(false);
        }}
      >
        <input
          type="password"
          autoFocus
          autoComplete="off"
          placeholder="Approver API key"
          aria-label="Approver API key"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="w-52 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-950"
        />
        <Button type="submit" variant="primary">
          Save
        </Button>
        <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
          Cancel
        </Button>
      </form>
    );
  }
  return key ? (
    <div className="flex items-center gap-2 text-sm">
      <span className="text-emerald-700 dark:text-emerald-400">● Approver key set</span>
      <Button variant="ghost" onClick={() => setApproverKey(null)}>
        Clear
      </Button>
    </div>
  ) : (
    <Button onClick={() => setEditing(true)}>Set approver key</Button>
  );
}
