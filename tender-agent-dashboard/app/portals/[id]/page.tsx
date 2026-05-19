import PortalDetail from "@/components/PortalDetail";

export default async function PortalDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <PortalDetail id={Number(id)} />;
}
