export interface StatusDescriptor { glyph: string; label: string }
export declare const villaniTokens: Readonly<Record<string, string>>;
export declare const chartTokens: Readonly<Record<string, string>>;
export declare const statusDescriptors: Readonly<Record<string, StatusDescriptor>>;
export declare const uiClassNames: Readonly<Record<string, string>>;
export declare const villaniThemeCss: string;
export declare function statusDescriptor(status?: string | null): StatusDescriptor;
