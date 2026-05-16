import { notFound } from "next/navigation";

import VaultDocumentDetail from "@/components/VaultDocumentDetail";

export default async function VaultDocumentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const numeric = Number(id);
  if (!Number.isInteger(numeric) || numeric < 1) {
    notFound();
  }
  return <VaultDocumentDetail id={numeric} />;
}
