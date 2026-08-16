import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { DataTable } from "./DataTable";
import type { ColumnDef } from "./DataTable";
import { FkCell, GaugeCell } from "./cells";
import { SeverityPill } from "./SeverityPill";

// This repo runs Vitest with `globals: false`, so React Testing Library cannot
// auto-register its own afterEach cleanup — without this each test's render
// leaks into the next and `screen` queries match duplicated elements.
afterEach(cleanup);

type Row = {
  hostname: string;
  domain: string | null;
  severity: string;
  cvss: number | null;
};

const rows: Row[] = [
  { hostname: "TNDSKCG2QCX3", domain: "windows", severity: "critical", cvss: 9.8 },
  { hostname: "deploy-host-01", domain: null, severity: "medium", cvss: null },
];

const columns: ColumnDef<Row>[] = [
  { key: "hostname", header: "hostname", typeGlyph: "pk" },
  { key: "domain", header: "domain", typeGlyph: "enum" },
  {
    key: "severity",
    header: "severity",
    typeGlyph: "enum",
    render: (r) => <SeverityPill severity={r.severity} />,
  },
  {
    key: "cvss",
    header: "cvss",
    typeGlyph: "num",
    render: (r) => (r.cvss == null ? null : <GaugeCell value={r.cvss * 10} severity={r.severity} display={r.cvss} />),
  },
];

describe("DataTable", () => {
  it("renders every column header with its type glyph", () => {
    render(<DataTable columns={columns} rows={rows} caption="vulns" />);
    const headers = screen.getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent)).toEqual([
      "PKhostname",
      "◆domain",
      "◆severity",
      "#cvss",
    ]);
  });

  it("marks the primary-key glyph distinctly", () => {
    const { container } = render(<DataTable columns={columns} rows={rows} />);
    const glyphs = container.querySelectorAll(".ib-tg");
    expect(glyphs[0]).toHaveClass("ib-tg-pk");
    expect(glyphs[1]).not.toHaveClass("ib-tg-pk");
  });

  it("renders one body row per data row", () => {
    render(<DataTable columns={columns} rows={rows} />);
    expect(screen.getByText("TNDSKCG2QCX3")).toBeInTheDocument();
    expect(screen.getByText("deploy-host-01")).toBeInTheDocument();
  });

  it("renders the NULL token instead of a blank cell for a missing value", () => {
    const { container } = render(<DataTable columns={columns} rows={rows} />);
    // row 2 has a null `domain` (default accessor path) and a null `cvss`
    // (custom render returning null) — both must show the token.
    const nulls = container.querySelectorAll(".ib-null");
    expect(nulls).toHaveLength(2);
    expect(nulls[0]).toHaveTextContent("NULL");
  });

  it("does not NULL-out falsy-but-real values like 0 and false", () => {
    type N = { count: number; active: boolean };
    const { container } = render(
      <DataTable
        columns={[
          { key: "count", header: "count", typeGlyph: "num" },
          { key: "active", header: "active" },
        ]}
        rows={[{ count: 0, active: false } as N]}
      />,
    );
    expect(container.querySelectorAll(".ib-null")).toHaveLength(0);
    expect(screen.getByText("0")).toBeInTheDocument();
    expect(screen.getByText("false")).toBeInTheDocument();
  });

  it("right-aligns numeric columns", () => {
    const { container } = render(<DataTable columns={columns} rows={rows} />);
    const firstRow = container.querySelectorAll("tbody tr")[0];
    const cells = firstRow.querySelectorAll("td");
    expect(cells[3]).toHaveClass("ib-num");
    expect(cells[0]).not.toHaveClass("ib-num");
  });

  it("renders severity pills with the class for each level", () => {
    const { container } = render(<DataTable columns={columns} rows={rows} />);
    const pills = container.querySelectorAll(".ib-sev");
    expect(pills[0]).toHaveClass("ib-sev-crit");
    expect(pills[1]).toHaveClass("ib-sev-med");
  });

  it("shows an empty message rather than an empty grid", () => {
    render(<DataTable columns={columns} rows={[]} emptyMessage="No vulnerabilities." />);
    expect(screen.getByText("No vulnerabilities.")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "No vulnerabilities." })).toHaveAttribute("colspan", "4");
  });

  describe("density toggle", () => {
    it("starts at compact with aria-pressed on the active button", () => {
      const { container } = render(<DataTable columns={columns} rows={rows} />);
      expect(container.querySelector(".ib-dt")).toHaveAttribute("data-density", "compact");
      expect(screen.getByRole("button", { name: "Compact" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByRole("button", { name: "Cozy" })).toHaveAttribute("aria-pressed", "false");
    });

    it("changes real state (and therefore data-density) when clicked", () => {
      const { container } = render(<DataTable columns={columns} rows={rows} />);
      fireEvent.click(screen.getByRole("button", { name: "Tall" }));
      expect(container.querySelector(".ib-dt")).toHaveAttribute("data-density", "tall");
      expect(screen.getByRole("button", { name: "Tall" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByRole("button", { name: "Compact" })).toHaveAttribute("aria-pressed", "false");
    });

    it("honours defaultDensity", () => {
      const { container } = render(<DataTable columns={columns} rows={rows} defaultDensity="cozy" />);
      expect(container.querySelector(".ib-dt")).toHaveAttribute("data-density", "cozy");
    });

    it("supports controlled density via onDensityChange", () => {
      const onDensityChange = vi.fn();
      const { container } = render(
        <DataTable columns={columns} rows={rows} density="cozy" onDensityChange={onDensityChange} />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Tall" }));
      expect(onDensityChange).toHaveBeenCalledWith("tall");
      // Controlled: the prop still governs until the parent updates it.
      expect(container.querySelector(".ib-dt")).toHaveAttribute("data-density", "cozy");
    });

    it("exposes the segmented control as a labelled group", () => {
      render(<DataTable columns={columns} rows={rows} />);
      expect(screen.getByRole("group", { name: "Row height" })).toBeInTheDocument();
    });

    it("can be hidden", () => {
      render(<DataTable columns={columns} rows={rows} showDensityToggle={false} />);
      expect(screen.queryByRole("button", { name: "Compact" })).toBeNull();
    });
  });

  describe("toolbar and status bar", () => {
    it("renders the table name and filter chips", () => {
      render(<DataTable columns={columns} rows={rows} toolbarName="vulnerabilities" chips={["severity ≥ medium"]} />);
      expect(screen.getByText("vulnerabilities")).toHaveClass("ib-tname");
      expect(screen.getByText("severity ≥ medium")).toHaveClass("ib-tchip");
    });

    it("renders the shown/total/ms status strip with the read-only note", () => {
      const { container } = render(
        <DataTable columns={columns} rows={rows} statusBar={{ shown: 8, total: 1247, ms: 38 }} />,
      );
      const bar = container.querySelector(".ib-statusbar") as HTMLElement;
      expect(within(bar).getByText("8 of 1,247 rows")).toBeInTheDocument();
      expect(within(bar).getByText("38 ms")).toBeInTheDocument();
      expect(within(bar).getByText(/read-only/)).toBeInTheDocument();
    });

    it("defaults shown to the row count and omits the status bar when not asked for", () => {
      const withBar = render(<DataTable columns={columns} rows={rows} statusBar={{}} />);
      expect(withBar.getByText("2 rows")).toBeInTheDocument();
      withBar.unmount();
      const without = render(<DataTable columns={columns} rows={rows} />);
      expect(without.container.querySelector(".ib-statusbar")).toBeNull();
    });
  });

  describe("sorting", () => {
    it("is off by default — headers are plain, aria-sort absent", () => {
      render(<DataTable columns={columns} rows={rows} />);
      expect(screen.getAllByRole("columnheader")[0]).not.toHaveAttribute("aria-sort");
    });

    it("sorts ascending then descending and reports aria-sort", () => {
      const { container } = render(<DataTable columns={columns} rows={rows} sortable />);
      const hostHeader = screen.getAllByRole("columnheader")[0];
      expect(hostHeader).toHaveAttribute("aria-sort", "none");

      fireEvent.click(screen.getByRole("button", { name: /hostname/ }));
      expect(hostHeader).toHaveAttribute("aria-sort", "ascending");
      let first = container.querySelectorAll("tbody tr td")[0];
      expect(first).toHaveTextContent("deploy-host-01");

      fireEvent.click(screen.getByRole("button", { name: /hostname/ }));
      expect(hostHeader).toHaveAttribute("aria-sort", "descending");
      first = container.querySelectorAll("tbody tr td")[0];
      expect(first).toHaveTextContent("TNDSKCG2QCX3");
    });

    it("puts nullish values last regardless of direction, matching useSort", () => {
      const { container } = render(
        <DataTable
          columns={[{ key: "cvss", header: "cvss", typeGlyph: "num" }]}
          rows={rows}
          sortable
          initialSortKey="cvss"
        />,
      );
      const cells = container.querySelectorAll("tbody tr td");
      expect(cells[0]).toHaveTextContent("9.8");
      expect(cells[1]).toHaveTextContent("NULL");
    });

    it("puts nullish values last on an initial descending sort too", () => {
      // Regression test: descending sort used to be implemented as
      // "sort ascending, then reverse()", which put nulls FIRST on
      // descending instead of last (contradicting the nulls-last contract
      // documented on compareValues). Mirrors how Vulns.tsx configures this
      // table (`initialSortDir="desc"` on a nullable column).
      const { container } = render(
        <DataTable
          columns={[{ key: "cvss", header: "cvss", typeGlyph: "num" }]}
          rows={rows}
          sortable
          initialSortKey="cvss"
          initialSortDir="desc"
        />,
      );
      const cells = container.querySelectorAll("tbody tr td");
      expect(cells[0]).toHaveTextContent("9.8");
      expect(cells[1]).toHaveTextContent("NULL");
    });

    it("reverses the non-null ordering when toggled to descending, with nulls still last", () => {
      type R = { label: string; n: number | null };
      const nRows: R[] = [
        { label: "a", n: 1 },
        { label: "b", n: 3 },
        { label: "c", n: null },
        { label: "d", n: 2 },
      ];
      const { container } = render(
        <DataTable
          columns={[
            { key: "label", header: "label" },
            { key: "n", header: "n", typeGlyph: "num" },
          ]}
          rows={nRows}
          sortable
        />,
      );
      // Toggle the "n" column to ascending, then again to descending.
      fireEvent.click(screen.getByRole("button", { name: /^n/ }));
      fireEvent.click(screen.getByRole("button", { name: /^n/ }));
      const labels = Array.from(container.querySelectorAll("tbody tr")).map(
        (tr) => tr.querySelector("td")?.textContent,
      );
      expect(labels).toEqual(["b", "d", "a", "c"]);
    });

    it("excludes columns marked sortable={false}", () => {
      render(
        <DataTable
          columns={[{ key: "hostname", header: "hostname", sortable: false }]}
          rows={rows}
          sortable
        />,
      );
      expect(screen.queryByRole("button", { name: /hostname/ })).toBeNull();
    });
  });

  it("fires onRowClick with the clicked row", () => {
    const onRowClick = vi.fn();
    render(<DataTable columns={columns} rows={rows} onRowClick={onRowClick} />);
    fireEvent.click(screen.getByText("TNDSKCG2QCX3").closest("tr") as HTMLElement);
    expect(onRowClick).toHaveBeenCalledWith(rows[0], 0);
  });

  // F-9: the detail drawers on Drift/Hosts/CollRuns are the ONLY path to most
  // detail data, and a clickable <tr onClick> with no tabIndex/key handler is
  // unreachable by keyboard.
  it("F-9: a clickable row is keyboard-focusable and Enter fires onRowClick", () => {
    const onRowClick = vi.fn();
    render(<DataTable columns={columns} rows={rows} onRowClick={onRowClick} />);
    const tr = screen.getByText("TNDSKCG2QCX3").closest("tr") as HTMLElement;

    expect(tr.tabIndex).toBe(0);

    fireEvent.keyDown(tr, { key: "Enter" });
    expect(onRowClick).toHaveBeenCalledWith(rows[0], 0);
  });

  it("F-9: the space key also fires onRowClick on a clickable row", () => {
    const onRowClick = vi.fn();
    render(<DataTable columns={columns} rows={rows} onRowClick={onRowClick} />);
    const tr = screen.getByText("TNDSKCG2QCX3").closest("tr") as HTMLElement;

    fireEvent.keyDown(tr, { key: " " });
    expect(onRowClick).toHaveBeenCalledWith(rows[0], 0);
  });

  it("F-9: a non-clickable row (no onRowClick) is not put in the tab order", () => {
    render(<DataTable columns={columns} rows={rows} />);
    const tr = screen.getByText("TNDSKCG2QCX3").closest("tr") as HTMLElement;
    expect(tr.tabIndex).toBe(-1);
  });

  it("routes FK cells through FkCell so a raw id is never the primary text", () => {
    const onClick = vi.fn();
    render(
      <DataTable
        columns={[
          {
            key: "hostname",
            header: "host",
            typeGlyph: "fk",
            render: (r) => <FkCell label={r.hostname} id="uuid-1" onClick={onClick} />,
          },
        ]}
        rows={[rows[0]]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /TNDSKCG2QCX3/ }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
