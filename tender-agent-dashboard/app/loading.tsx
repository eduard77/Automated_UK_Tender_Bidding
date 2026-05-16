// Global route loading fallback — sits at the App Router root so any
// async page boundary uses Elevated Genera's shimmer skeleton.

export default function Loading() {
  return (
    <div className="space-y-12 py-14" aria-busy="true" aria-label="Loading">
      <div className="space-y-5">
        <div className="shimmer rounded-pill h-7 w-72" />
        <div className="shimmer rounded-md h-16 w-3/4" />
        <div className="shimmer rounded-md h-5 w-2/3" />
      </div>
      <div className="shimmer rounded-2xl" style={{ height: "120px", borderRadius: "18px" }} />
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            className="shimmer rounded-2xl"
            style={{ height: "260px", borderRadius: "18px" }}
          />
        ))}
      </div>
    </div>
  );
}
