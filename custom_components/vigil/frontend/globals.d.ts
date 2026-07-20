// Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
// All rights reserved.

// Ambient declarations for browser APIs the card uses that the default TS DOM
// lib doesn't yet ship: the HA custom-card registry and Intl.DurationFormat.

export {};

declare global {
  interface Window {
    customCards?: Array<{
      type: string;
      name: string;
      description: string;
      preview: boolean;
    }>;
  }

  namespace Intl {
    class DurationFormat {
      constructor(
        locales?: string | string[],
        options?: { style?: "long" | "short" | "narrow" | "digital" },
      );
      format(duration: Record<string, number>): string;
    }
  }
}
