export type Snapshot = {
  rows: Record<string, any>[];
  cursor: string | null;
  hasMore: boolean;
  updatedAt: number;
};

const prefix = "teammanager:snapshot:v1:";

export function readSnapshot(key: string): Snapshot | null {
  try {
    const value = JSON.parse(localStorage.getItem(prefix + key) || "null");
    if (
      !value || !Array.isArray(value.rows) ||
      !value.rows.every((row: unknown) => row && typeof row === "object" && !Array.isArray(row)) ||
      typeof value.updatedAt !== "number" || !Number.isFinite(value.updatedAt) ||
      typeof value.hasMore !== "boolean" ||
      !(value.cursor === null || typeof value.cursor === "string")
    ) return null;
    return value;
  } catch {
    return null;
  }
}

export function writeSnapshot(key: string, value: Snapshot) {
  try {
    localStorage.setItem(prefix + key, JSON.stringify(value));
  } catch {
    throw new Error("无法保存本次数据，请检查浏览器存储空间；上次保存的数据仍保留。");
  }
}

export function removeSpaceSnapshots(id: number, workspaceId: string) {
  for (const tab of ["users", "invites", "probes", "audit", "reservations"]) {
    localStorage.removeItem(prefix + JSON.stringify([id, workspaceId, tab]));
  }
}
