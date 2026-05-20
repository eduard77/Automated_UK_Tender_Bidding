import PlatformDetail from "@/components/PlatformDetail";

export default async function PlatformDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  return <PlatformDetail slug={slug} />;
}
