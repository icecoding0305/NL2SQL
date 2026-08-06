import type { ComponentProps } from "react";
import { StepCard } from "../pages/QueryPage";

export default function PipelineStepCard(props: ComponentProps<typeof StepCard>) {
  return <StepCard {...props} />;
}
