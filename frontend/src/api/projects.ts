import { apiV2 } from "./client.v2";
import type { components } from "./generated";

export type Project = components["schemas"]["ProjectListOut"];
export type ProjectDetail = components["schemas"]["ProjectDetailOut"];
export type ProjectContextEntry = components["schemas"]["ProjectContextEntryOut"];
export type ProjectContext = components["schemas"]["ProjectContextOut"];
export type ProjectSlug = components["schemas"]["ProjectSlugOut"];

export const projectsApi = {
  list: async (): Promise<Project[]> => {
    const { data, error } = await apiV2.GET("/api/projects/");
    if (error) throw new Error("Failed to load projects");
    return data.items as Project[];
  },
  listSlugs: async (): Promise<ProjectSlug[]> => {
    const { data, error } = await apiV2.GET("/api/projects/slugs/");
    if (error) throw new Error("Failed to load project slugs");
    return Array.from(data) as ProjectSlug[];
  },
  get: async (slug: string): Promise<ProjectDetail> => {
    const { data, error } = await apiV2.GET("/api/projects/{slug}/", {
      params: { path: { slug } },
    });
    if (error) throw new Error("Failed to load project");
    return data as unknown as ProjectDetail;
  },
  create: async (input: {
    name: string;
    slug: string;
    repo_url?: string;
    deploy_url?: string;
    visibility?: string;
    status?: string;
  }): Promise<ProjectDetail> => {
    const { data, error } = await apiV2.POST("/api/projects/", {
      body: {
        name: input.name,
        slug: input.slug,
        repo_url: input.repo_url ?? "",
        deploy_url: input.deploy_url ?? "",
        visibility: (input.visibility as "public" | "private") ?? "public",
        status: (input.status as "active" | "stale" | "archived") ?? "active",
      },
    });
    if (error) throw new Error("Failed to create project");
    return data as unknown as ProjectDetail;
  },
  update: async (
    slug: string,
    input: Partial<{
      name: string;
      repo_url: string;
      deploy_url: string;
      status: string;
      visibility: string;
    }>,
  ): Promise<ProjectDetail> => {
    const { data, error } = await apiV2.PATCH("/api/projects/{slug}/", {
      params: { path: { slug } },
      body: {
        name: input.name ?? null,
        repo_url: input.repo_url ?? null,
        deploy_url: input.deploy_url ?? null,
        visibility: (input.visibility as "public" | "private" | null) ?? null,
        status: (input.status as "active" | "stale" | "archived" | null) ?? null,
      },
    });
    if (error) throw new Error("Failed to update project");
    return data as unknown as ProjectDetail;
  },
  delete: async (slug: string): Promise<void> => {
    const { error } = await apiV2.DELETE("/api/projects/{slug}/", {
      params: { path: { slug } },
    });
    if (error) throw new Error("Failed to delete project");
  },
  postContext: async (
    slug: string,
    input: { context_type: string; content: string; source: string },
  ): Promise<ProjectContextEntry> => {
    const { data, error } = await apiV2.POST("/api/projects/{slug}/context/", {
      params: { path: { slug } },
      body: {
        context_type: input.context_type as
          | "current_work"
          | "next_step"
          | "summary"
          | "note",
        content: input.content,
        source: input.source,
      },
    });
    if (error) throw new Error("Failed to create context entry");
    return data;
  },
  getContext: async (slug: string): Promise<ProjectContextEntry[]> => {
    const { data, error } = await apiV2.GET("/api/projects/{slug}/context/", {
      params: { path: { slug } },
    });
    if (error) throw new Error("Failed to load context");
    return data as unknown as ProjectContextEntry[];
  },
  getLatestContext: async (
    slug: string,
  ): Promise<Record<string, ProjectContext>> => {
    const { data, error } = await apiV2.GET(
      "/api/projects/{slug}/context/latest/",
      {
        params: { path: { slug } },
      },
    );
    if (error) throw new Error("Failed to load latest context");
    return data.contexts as Record<string, ProjectContext>;
  },
}
