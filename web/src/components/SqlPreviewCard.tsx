import { SqlCard } from "./StepCards";

export default function SqlPreviewCard(props: { sql: string; traceId: string; node: string }) {
  return <SqlCard {...props} />;
}
