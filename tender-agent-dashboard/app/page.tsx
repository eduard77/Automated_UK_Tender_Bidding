import TenderList from "@/components/TenderList";

export default function HomePage() {
  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="font-display text-3xl text-bone">Tenders</h1>
        <p className="text-sm text-bone/50 font-mono uppercase tracking-wider">
          UK public-sector opportunities · live discovery
        </p>
      </header>
      <TenderList />
    </div>
  );
}
