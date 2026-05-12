import Link from "next/link";
import { type Tender, daysUntil, formatValue } from "@/lib/api";

const SOURCE_LABEL: Record<string, string> = {
  FTS: "Find a Tender",
  CF: "Contracts Finder",
  PCS: "Public Contracts Scotland",
  S2W: "Sell2Wales",
  NI: "eTendersNI",
};

export default function TenderCard({ tender }: { tender: Tender }) {
  const days = daysUntil(tender.deadline_at);
  const urgent = days !== null && days <= 7;

  return (
    <Link
      href={`/tenders/${tender.id}`}
      className="group block border border-bone/10 hover:border-rust transition bg-ink-soft/40 hover:bg-ink-soft/70"
    >
      <article className="p-5 relative">
        <div className="flex items-start justify-between gap-4 mb-3">
          <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono uppercase tracking-widest">
            <span className="text-rust">{SOURCE_LABEL[tender.source_code] || tender.source_code}</span>
            {tender.notice_type && (
              <>
                <span className="text-bone/30">·</span>
                <span className="text-bone/50">{tender.notice_type}</span>
              </>
            )}
            {tender.status && tender.status !== "active" && (
              <>
                <span className="text-bone/30">·</span>
                <span className="text-sage/80">{tender.status}</span>
              </>
            )}
          </div>
          {days !== null && (
            <div className={`text-xs font-mono whitespace-nowrap ${urgent ? "text-rust" : "text-bone/60"}`}>
              {days < 0 ? "closed" : days === 0 ? "today" : `${days}d left`}
            </div>
          )}
        </div>

        <h2 className="font-display text-xl leading-tight text-bone group-hover:text-rust transition mb-2">
          {tender.title}
        </h2>

        {tender.buyer_name && (
          <p className="text-sm text-bone/60 mb-3 italic">{tender.buyer_name}</p>
        )}

        {tender.description && (
          <p className="text-sm text-bone/70 line-clamp-2 mb-4">{tender.description}</p>
        )}

        <div className="flex items-end justify-between gap-4 mt-4 pt-4 border-t border-bone/10">
          <div className="font-mono text-bone">
            {formatValue(tender.value_amount, tender.value_currency)}
          </div>
          <div className="text-[10px] font-mono uppercase tracking-widest text-bone/40">
            {tender.cpv_codes && tender.cpv_codes.length > 0 ? `cpv ${tender.cpv_codes[0]}` : ""}
          </div>
        </div>
      </article>
    </Link>
  );
}
