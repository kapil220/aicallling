import { BRAND_NAME } from "@/constants/brand";
import { cn } from "@/lib/utils";

// Text wordmark placeholder until a real logo asset exists. Renders BRAND_NAME
// as a styled span. `inverse` forces light text (e.g. on the always-dark auth
// brand panel); `mark` renders a compact initial-only badge for tight spaces
// (e.g. the collapsed app sidebar header). Height is controlled by the caller
// via className (e.g. "h-7") to keep call sites unchanged; text size is derived
// from that height class where possible, otherwise falls back to a sensible
// default.
export function BrandLogo({
  className,
  inverse = false,
  mark = false,
}: {
  className?: string;
  inverse?: boolean;
  mark?: boolean;
}) {
  if (mark) {
    return (
      <span
        className={cn(
          "inline-flex select-none items-center justify-center rounded-md bg-primary px-1.5 font-bold text-primary-foreground",
          className,
        )}
        aria-label={BRAND_NAME}
      >
        {BRAND_NAME.charAt(0)}
      </span>
    );
  }

  return (
    <span
      className={cn(
        "inline-flex select-none items-center text-xl font-bold leading-none",
        inverse ? "text-white" : "text-foreground",
        className,
      )}
      aria-label={BRAND_NAME}
    >
      {BRAND_NAME}
    </span>
  );
}
