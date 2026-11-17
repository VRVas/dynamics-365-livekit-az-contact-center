'use client';

import { useMemo, useState } from 'react';
import { motion } from 'framer-motion';
import { useLocale } from '@/i18n/LocaleProvider';

/* eslint-disable @typescript-eslint/no-explicit-any */

// Recursively flatten a hast node to its text content.
function hastText(node: any): string {
  if (!node) return '';
  if (node.type === 'text') return node.value || '';
  if (Array.isArray(node.children)) return node.children.map(hastText).join('');
  return '';
}

interface TableRow {
  cells: string[];
  header: boolean;
}

// Walk the hast <table> node and pull out header + body rows as plain text.
function collectRows(node: any): TableRow[] {
  const rows: TableRow[] = [];
  const visit = (n: any) => {
    if (!n || !Array.isArray(n.children)) return;
    for (const child of n.children) {
      if (child.tagName === 'tr') {
        const cells: string[] = [];
        let header = false;
        for (const cell of child.children || []) {
          if (cell.tagName === 'th' || cell.tagName === 'td') {
            if (cell.tagName === 'th') header = true;
            cells.push(hastText(cell).trim());
          }
        }
        if (cells.length) rows.push({ cells, header });
      } else {
        visit(child);
      }
    }
  };
  visit(node);
  return rows;
}

// Numeric-aware comparison so currency / numbers sort correctly.
function compareCells(a: string, b: string): number {
  const na = parseFloat(a.replace(/[^0-9.\-]/g, ''));
  const nb = parseFloat(b.replace(/[^0-9.\-]/g, ''));
  const aNum = /[0-9]/.test(a) && !isNaN(na);
  const bNum = /[0-9]/.test(b) && !isNaN(nb);
  if (aNum && bNum) return na - nb;
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

export function InteractiveTable({ node }: { node: any }) {
  const { t } = useLocale();
  const { headers, rows } = useMemo(() => {
    const all = collectRows(node);
    const headerRow = all.find((r) => r.header) || all[0];
    const bodyRows = all.filter((r) => r !== headerRow);
    const cols = headerRow?.cells.length || (bodyRows[0]?.cells.length ?? 0);
    return {
      headers: headerRow?.cells ?? Array.from({ length: cols }, (_, i) => t('Column {number}', { number: i + 1 })),
      rows: bodyRows.map((r) => r.cells),
    };
  }, [node, t]);

  const [sort, setSort] = useState<{ col: number; dir: 'asc' | 'desc' } | null>(null);

  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const res = compareCells(a[sort.col] ?? '', b[sort.col] ?? '');
      return sort.dir === 'asc' ? res : -res;
    });
    return copy;
  }, [rows, sort]);

  const toggleSort = (col: number) => {
    setSort((prev) => {
      if (!prev || prev.col !== col) return { col, dir: 'asc' };
      if (prev.dir === 'asc') return { col, dir: 'desc' };
      return null;
    });
  };

  if (!headers.length) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="md-table-wrap my-3"
    >
      <div className="md-table-scroll">
        <table className="md-itable">
          <thead>
            <tr>
              {headers.map((h, i) => (
                <th key={i} aria-sort={sort?.col === i ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'} className={sort?.col === i ? 'is-sorted' : ''}>
                  <button type="button" onClick={() => toggleSort(i)} aria-label={t('Sort by {column}', { column: h })}>
                  <span className="md-th-inner">
                    {h}
                    <span className={`md-sort ${sort?.col === i ? 'active' : ''}`}>
                      {sort?.col === i ? (sort.dir === 'asc' ? '\u2191' : '\u2193') : '\u21c5'}
                    </span>
                  </span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row, r) => (
              <tr key={r}>
                {headers.map((_, c) => (
                  <td key={c}>{row[c] ?? ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="md-table-foot">
        {t('{count} rows', { count: rows.length })}
        {sort && <span className="md-table-hint"> · {t('Sorted by {column}', { column: headers[sort.col] })}</span>}
      </div>
    </motion.div>
  );
}
