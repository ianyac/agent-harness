import type { ActivityItem } from "../../protocol/types";

export type ActivityGroup = readonly ActivityItem[];

function isRoutineActivity(activity: ActivityItem): boolean {
  return activity.actor === "tool" && activity.status !== "error" && activity.isError !== true;
}

export function groupActivities(activities: readonly ActivityItem[]): ActivityGroup[] {
  const groups: ActivityItem[][] = [];
  let routineGroup: ActivityItem[] = [];

  const finishRoutineGroup = () => {
    if (routineGroup.length > 0) groups.push(routineGroup);
    routineGroup = [];
  };

  for (const activity of activities) {
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
