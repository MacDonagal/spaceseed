const rowData = JSON.parse(document.getElementById("opportunities-data").textContent);

const formatNumber = (params) => {
  if (params.value === null || params.value === undefined) return "—";
  return Number(params.value).toLocaleString("fr-BE");
};

const scoreRenderer = (params) => {
  const value = Number(params.value ?? 0);
  let level = "low";

  if (value >= 150) level = "excellent";
  else if (value >= 90) level = "good";
  else if (value >= 40) level = "medium";

  return `<span class="ag-score ag-score-${level}">${value.toFixed(1)}</span>`;
};

const freeleechRenderer = (params) => {
  return params.value
    ? '<span class="ag-badge ag-badge-freeleech">FreeLeech</span>'
    : '<span class="ag-muted">Non</span>';
};

const actionRenderer = (params) => {
  if (!params.data.download_available) {
    return '<span class="ag-muted">Indisponible</span>';
  }

  return `
    <button
      class="ag-download-button"
      type="button"
      data-download-id="${params.data.id}"
    >
      Envoyer à qBittorrent
    </button>
  `;
};

const normalizeTorrentName = (v) =>
  String(v ?? "")
    .toLowerCase()
    .replace(/\.(torrent|mkv|mp4|avi)$/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
const formatSpeed = (v) => {
  const b = Number(v ?? 0);
  if (b <= 0) return "0 o/s";
  const u = ["o/s", "Kio/s", "Mio/s", "Gio/s"];
  const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), u.length - 1);
  return `${(b / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${u[i]}`;
};
const swarmRenderer = (p) =>
  p.data.qbit_health
    ? `<span class="swarm-status swarm-${p.data.qbit_health}">${p.data.qbit_health_label}</span>`
    : '<span class="swarm-status swarm-not-added">Non ajouté</span>';
const qbitRenderer = (p) =>
  p.data.qbit_health
    ? `<div class="qbit-cell"><strong>${(Number(p.data.qbit_progress) * 100).toFixed(1)} %</strong><span>${formatSpeed(p.data.qbit_progress >= 1 ? p.data.qbit_upload_speed : p.data.qbit_download_speed)}</span></div>`
    : '<span class="ag-muted">—</span>';

const gridOptions = {
  rowData,
  getRowId: (params) => String(params.data.id),

  columnDefs: [
    {
      field: "title",
      headerName: "Torrent",
      minWidth: 420,
      flex: 3,
      pinned: "left",
      tooltipField: "title",
      filter: "agTextColumnFilter",
    },
    {
      field: "score",
      headerName: "Score",
      width: 115,
      sort: "desc",
      cellRenderer: scoreRenderer,
      filter: "agNumberColumnFilter",
    },
    {
      field: "seeders",
      headerName: "Seeds",
      width: 105,
      valueFormatter: formatNumber,
      filter: "agNumberColumnFilter",
    },
    {
      field: "leechers",
      headerName: "Leechers",
      width: 120,
      valueFormatter: formatNumber,
      filter: "agNumberColumnFilter",
    },
    {
      field: "demand_ratio",
      headerName: "Demande / offre",
      width: 155,
      filter: "agNumberColumnFilter",
    },
    {
      field: "size_gb",
      headerName: "Taille",
      width: 115,
      valueFormatter: (params) => `${params.value} Go`,
      filter: "agNumberColumnFilter",
    },
    {
      field: "freeleech",
      headerName: "FreeLeech",
      width: 135,
      cellRenderer: freeleechRenderer,
      filter: true,
    },
    {
      field: "category",
      headerName: "Catégorie",
      width: 135,
      filter: "agTextColumnFilter",
    },
    {
      field: "last_seen",
      headerName: "Dernière vue",
      width: 145,
      filter: false,
    },
    {
      field: "qbit_health",
      headerName: "Swarm",
      width: 145,
      cellRenderer: swarmRenderer,
      filter: "agTextColumnFilter",
    },
    {
      field: "qbit_progress",
      headerName: "qBittorrent",
      width: 145,
      cellRenderer: qbitRenderer,
      filter: "agNumberColumnFilter",
    },
    {
      headerName: "Action",
      width: 215,
      sortable: false,
      filter: false,
      pinned: "right",
      cellRenderer: actionRenderer,
    },
  ],

  defaultColDef: {
    sortable: true,
    filter: true,
    resizable: true,
    floatingFilter: true,
    suppressHeaderMenuButton: false,
  },

  theme: agGrid.themeQuartz.withParams({
    accentColor: "#72f1b8",
    backgroundColor: "#0b1728",
    browserColorScheme: "dark",
    chromeBackgroundColor: "#102139",
    foregroundColor: "#e8f1ff",
    headerBackgroundColor: "#0d1b2e",
    headerTextColor: "#91a7c2",
    borderColor: "#1d3550",
    rowBorder: { color: "#1d3550" },
    oddRowBackgroundColor: "#0d1b2e",
    selectedRowBackgroundColor: "rgba(114, 241, 184, 0.12)",
    wrapperBorderRadius: "18px",
    fontFamily: 'Inter, "Segoe UI", Arial, sans-serif',
    fontSize: 14,
    headerFontSize: 12,
    headerFontWeight: 800,
    spacing: 8,
  }),

  pagination: true,
  paginationPageSize: 50,
  paginationPageSizeSelector: [25, 50, 100, 200],

  rowHeight: 52,
  headerHeight: 46,
  floatingFiltersHeight: 42,
  animateRows: true,
  enableCellTextSelection: true,
  ensureDomOrder: true,

  onGridReady: (params) => {
    window.opportunitiesGridApi = params.api;

    const savedState = localStorage.getItem("spaceseed-grid-state");
    if (savedState) {
      try {
        params.api.applyColumnState({
          state: JSON.parse(savedState),
          applyOrder: true,
        });
      } catch (error) {
        console.warn("État AG Grid invalide", error);
      }
    }

    updateVisibleCount(params.api);
  },

  onColumnMoved: saveGridState,
  onColumnResized: saveGridState,
  onSortChanged: saveGridState,
  onFilterChanged: (event) => updateVisibleCount(event.api),
  onPaginationChanged: (event) => updateVisibleCount(event.api),
};

function saveGridState(event) {
  if (!event.api) return;
  localStorage.setItem("spaceseed-grid-state", JSON.stringify(event.api.getColumnState()));
}

function updateVisibleCount(api) {
  document.getElementById("visible-count").textContent = api
    .getDisplayedRowCount()
    .toLocaleString("fr-BE");
}

async function refreshQBittorrentStatuses() {
  try {
    const r = await fetch("/api/qbittorrent/torrents", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const payload = await r.json(),
      byName = new Map((payload.torrents ?? []).map((t) => [normalizeTorrentName(t.name), t])),
      updates = [];
    window.opportunitiesGridApi.forEachNode((node) => {
      const t = byName.get(normalizeTorrentName(node.data.title));
      if (!t && !node.data.qbit_health) return;
      updates.push({
        ...node.data,
        qbit_health: t?.health,
        qbit_health_label: t?.health_label,
        qbit_progress: t?.progress,
        qbit_download_speed: t?.download_speed,
        qbit_upload_speed: t?.upload_speed,
        qbit_ratio: t?.ratio,
      });
    });
    if (updates.length) window.opportunitiesGridApi.applyTransaction({ update: updates });
  } catch (e) {
    console.warn("Statut qBittorrent indisponible", e);
  }
}

const gridElement = document.getElementById("opportunities-grid");
agGrid.createGrid(gridElement, gridOptions);
setTimeout(refreshQBittorrentStatuses, 400);
setInterval(refreshQBittorrentStatuses, 15000);

document.getElementById("grid-search").addEventListener("input", (event) => {
  window.opportunitiesGridApi.setGridOption("quickFilterText", event.target.value);
  updateVisibleCount(window.opportunitiesGridApi);
});

document.getElementById("reset-grid").addEventListener("click", () => {
  localStorage.removeItem("spaceseed-grid-state");
  document.getElementById("grid-search").value = "";
  window.opportunitiesGridApi.setGridOption("quickFilterText", "");
  window.opportunitiesGridApi.setFilterModel(null);
  window.opportunitiesGridApi.resetColumnState();
  window.opportunitiesGridApi.applyColumnState({
    state: [{ colId: "score", sort: "desc" }],
  });
  updateVisibleCount(window.opportunitiesGridApi);
});

gridElement.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-download-id]");
  if (!button) return;

  const opportunityId = button.dataset.downloadId;
  const originalText = button.textContent;

  button.disabled = true;
  button.textContent = "Envoi…";

  try {
    const response = await fetch(`/download/${opportunityId}`, {
      method: "POST",
      redirect: "follow",
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail ?? `HTTP ${response.status}`);
    }

    button.textContent = "Envoyé ✅";
    button.classList.add("sent");
    setTimeout(refreshQBittorrentStatuses, 1500);

    setTimeout(() => {
      button.disabled = false;
      button.textContent = originalText;
      button.classList.remove("sent");
    }, 2500);
  } catch (error) {
    console.error(error);
    button.textContent = "Erreur ❌";
    button.title = error.message;
    button.classList.add("failed");

    setTimeout(() => {
      button.disabled = false;
      button.textContent = originalText;
      button.classList.remove("failed");
      button.removeAttribute("title");
    }, 3000);
  }
});
