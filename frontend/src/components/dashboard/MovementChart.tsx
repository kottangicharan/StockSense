'use client';

import { useMemo } from 'react';
import useSWR from 'swr';
import { Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { qs } from '@/lib/api';
import { fmtDate, fmtQty } from '@/lib/constants';
import type { Move } from '@/lib/types';

// Validated against the dark surface (dataviz validator). Red/green is weak under deuteranopia,
// so "Out" is also dashed and the legend is always shown.
const IN = 'rgb(var(--ok))';
const OUT = 'rgb(var(--accent))';

/** Daily quantity received vs delivered, from done moves (/moves is readable by every role; /ledger is manager-only). */
export function MovementChart({ warehouseId = '' }: { warehouseId?: string }) {
  const { data, error } = useSWR<Move[]>(qs('/moves', { status: 'done', warehouse_id: warehouseId, limit: 2000 }));

  const series = useMemo(() => {
    const byDay = new Map<string, { day: string; in: number; out: number }>();
    for (const m of data ?? []) {
      if (m.type !== 'receive' && m.type !== 'delivery') continue;
      const d = byDay.get(m.scheduled_date) ?? { day: m.scheduled_date, in: 0, out: 0 };
      if (m.type === 'receive') d.in += m.qty; else d.out += m.qty;
      byDay.set(m.scheduled_date, d);
    }
    if (!byDay.size) return [];
    // Zero-fill a continuous range (at least the last 14 days) so a single active day still draws a line.
    // ponytail: range grows with the oldest move; clamp to a window if history gets long.
    const iso = (t: number) => new Date(t).toISOString().slice(0, 10);
    const DAY = 86_400_000;
    const days = [...byDay.keys()].sort();
    const today = Date.parse(new Date().toISOString().slice(0, 10));
    const start = Math.min(Date.parse(days[0]), today - 13 * DAY);
    const end = Math.max(Date.parse(days[days.length - 1]), today);
    const out = [];
    for (let t = start; t <= end; t += DAY) out.push(byDay.get(iso(t)) ?? { day: iso(t), in: 0, out: 0 });
    return out;
  }, [data]);

  return (
    <section className="rounded-lg border border-b1 bg-s0 p-4">
      <h2 className="mb-3 text-sm font-medium">Stock movement <span className="text-t3">· received vs delivered per day</span></h2>
      <div className="h-64">
        {error ? <p className="py-20 text-center text-sm text-accent">{error.message}</p>
          : !data ? <p className="py-20 text-center text-sm text-t3">Loading…</p>
          : series.length === 0 ? <p className="py-20 text-center text-sm text-t3">No validated receipts or deliveries yet.</p>
          : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={series} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                <defs>
                  <linearGradient id="gIn" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={IN} stopOpacity={0.25} /><stop offset="100%" stopColor={IN} stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="gOut" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={OUT} stopOpacity={0.2} /><stop offset="100%" stopColor={OUT} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgb(var(--b1))" vertical={false} />
                <XAxis dataKey="day" tickFormatter={fmtDate} stroke="rgb(var(--t3))" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis stroke="rgb(var(--t3))" fontSize={11} tickLine={false} axisLine={false} tickFormatter={fmtQty} />
                <Tooltip
                  cursor={{ stroke: 'rgb(var(--b3))' }}
                  contentStyle={{ background: 'rgb(var(--s2))', border: '1px solid rgb(var(--b3))', borderRadius: 8, fontSize: 12, color: 'rgb(var(--t1))' }}
                  labelFormatter={(d) => fmtDate(String(d))}
                  formatter={(v, name) => [fmtQty(Number(v)), name]}
                />
                <Legend iconType="plainline" wrapperStyle={{ fontSize: 12, color: 'rgb(var(--t2))' }} />
                <Area type="monotone" dataKey="in" name="Received" stroke={IN} strokeWidth={2} fill="url(#gIn)" dot={{ r: 3 }} activeDot={{ r: 5 }} />
                <Area type="monotone" dataKey="out" name="Delivered" stroke={OUT} strokeWidth={2} strokeDasharray="5 3" fill="url(#gOut)" dot={{ r: 3 }} activeDot={{ r: 5 }} />
              </AreaChart>
            </ResponsiveContainer>
          )}
      </div>
    </section>
  );
}
