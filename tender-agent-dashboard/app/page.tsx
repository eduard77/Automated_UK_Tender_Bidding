import TenderList from "@/components/TenderList";

export default function HomePage() {
  // The TenderList is a client component that owns header copy, stats,
  // featured tender + matched grid. Everything driven by live API.
  return <TenderList />;
}
