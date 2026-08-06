import { PlanCard } from "./StepCards";

export default function PlanPreviewCard(props: { plan: Record<string, any>; traceId: string }) {
  return <div className="preview-surface"><PlanCard {...props} /></div>;
}
