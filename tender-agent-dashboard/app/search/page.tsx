import SearchPage from "@/components/SearchPage";

export default function SearchRoute() {
  // Source-agnostic tender search/filter (Phase 4 chunk 7). The SearchPage is a
  // client component that owns the filter panel, click-to-search, results, and
  // pagination — all driven by /tenders/search + /tenders/facets.
  return <SearchPage />;
}
