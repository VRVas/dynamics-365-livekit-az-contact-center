import { NextResponse } from "next/server";
import { ZodError, ZodSchema, ZodIssue } from "zod";
import { getDeployment, type Feature } from "./deployment";

export function moduleUnavailable(feature: Feature): NextResponse | null {
  return getDeployment().features[feature] ? null : apiError("MODULE_DISABLED", `${feature} module is disabled.`, 503);
}

/**
 * Standard API response helper
 */
export function apiResponse<T>(data: T, status = 200) {
  return NextResponse.json(data, { status });
}

/**
 * Error response helper
 */
export function apiError(
  code: string,
  message: string,
  status = 400,
  details?: { field?: string; message: string }[]
) {
  const error = { error: { code, message, details } };
  return NextResponse.json(error, { status });
}

/**
 * Validation error from Zod
 */
export function validationError(error: ZodError) {
  const issues = error.issues as ZodIssue[];
  return apiError(
    "VALIDATION_ERROR",
    "Request validation failed",
    400,
    issues.map((e) => ({
      field: e.path.join("."),
      message: e.message,
    }))
  );
}

/**
 * Parse and validate request body
 */
export async function parseBody<T>(
  request: Request,
  schema: ZodSchema<T>
): Promise<{ data: T; error?: never } | { data?: never; error: NextResponse }> {
  try {
    const body = await request.json();
    const data = schema.parse(body);
    return { data };
  } catch (error) {
    if (error instanceof ZodError) {
      return { error: validationError(error) };
    }
    if (error instanceof SyntaxError) {
      return { error: apiError("INVALID_JSON", "Invalid JSON in request body", 400) };
    }
    return { error: apiError("PARSE_ERROR", "Failed to parse request body", 400) };
  }
}

/**
 * Parse and validate query parameters
 */
export function parseQuery<T>(
  searchParams: URLSearchParams,
  schema: ZodSchema<T>
): { data: T; error?: never } | { data?: never; error: NextResponse } {
  try {
    const params: Record<string, string> = {};
    searchParams.forEach((value, key) => {
      params[key] = value;
    });
    const data = schema.parse(params);
    return { data };
  } catch (error) {
    if (error instanceof ZodError) {
      return { error: validationError(error) };
    }
    return { error: apiError("PARSE_ERROR", "Failed to parse query parameters", 400) };
  }
}

/**
 * API Key authentication middleware
 */
export function validateApiKey(request: Request): boolean {
  const apiKey = request.headers.get("X-API-Key");
  const expectedKey = process.env.API_KEY;
  
  // In development, allow requests without API key
  if (process.env.NODE_ENV === "development" && !expectedKey) {
    return true;
  }
  
  return apiKey === expectedKey;
}

/**
 * Require API key authentication
 */
export function requireApiKey(request: Request): NextResponse | null {
  if (!validateApiKey(request)) {
    return apiError("UNAUTHORIZED", "Missing or invalid API key", 401);
  }
  return null;
}

/**
 * Standard error codes
 */
export const ErrorCodes = {
  VALIDATION_ERROR: "VALIDATION_ERROR",
  INVALID_JSON: "INVALID_JSON",
  PARSE_ERROR: "PARSE_ERROR",
  UNAUTHORIZED: "UNAUTHORIZED",
  NOT_FOUND: "NOT_FOUND",
  INTERNAL_ERROR: "INTERNAL_ERROR",
  DATABASE_ERROR: "DATABASE_ERROR",
} as const;
