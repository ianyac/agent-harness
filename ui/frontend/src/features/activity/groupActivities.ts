import type {
  ActivityItem,
  TranscriptAssistantTimelineItem,
  TranscriptBoundaryTimelineItem,
} from "../../protocol/types";

export type ActivityGroup = readonly ActivityItem[];
export type TimelineMarker = TranscriptAssistantTimelineItem | TranscriptBoundaryTimelineItem;
export type GroupedTimelineItem = ActivityGroup | TimelineMarker;

function isRoutineActivity(activity: ActivityItem): boolean {
  return activity.actor === "tool" && activity.status !== "error" && activity.isError !== true;
}

export function groupActivities(activities: readonly ActivityItem[]): ActivityGroup[];
export function groupActivities(items: readonly (ActivityItem | TimelineMarker)[]): GroupedTimelineItem[];
export function groupActivities(
  activities: readonly (ActivityItem | TimelineMarker)[],
): GroupedTimelineItem[] {
  const groups: GroupedTimelineItem[] = [];
  let routineGroup: ActivityItem[] = [];

  const finishRoutineGroup = () => {
    if (routineGroup.length > 0) groups.push(routineGroup);
    routineGroup = [];
  };

  for (const activity of activities) {
    if (!("activityId" in activity)) {
      finishRoutineGroup();
      groups.push(activity);
      continue;
    }
    if (!isRoutineActivity(activity)) {
      finishRoutineGroup();
      groups.push([activity]);
      continue;
    }

    if (
      routineGroup.length > 0 &&
      (routineGroup[routineGroup.length - 1].turnId !== activity.turnId ||
        routineGroup[routineGroup.length - 1].parentActivityId !== activity.parentActivityId)
    ) {
      finishRoutineGroup();
    }
    routineGroup.push(activity);
  }

  finishRoutineGroup();
  return groups;
}
