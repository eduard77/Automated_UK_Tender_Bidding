import FiltersManager from "@/components/FiltersManager";

export default function FiltersPage() {
  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="font-display text-3xl text-bone">Filters</h1>
        <p className="text-sm text-bone/50 font-mono uppercase tracking-wider">
          alerts · per-criteria push notifications
        </p>
      </header>
      <FiltersManager />
    </div>
  );
}
