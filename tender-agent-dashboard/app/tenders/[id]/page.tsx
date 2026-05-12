import { notFound } from "next/navigation";

import TenderDetail from "@/components/TenderDetail";

export default async function TenderDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const numeric = Number(id);
  if (!Number.isInteger(numeric) || numeric < 1) {
    notFound();
  }
  return <TenderDetail id={numeric} />;
}
