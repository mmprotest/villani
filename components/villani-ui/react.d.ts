import type * as React from "react";

export interface AppShellProps extends React.HTMLAttributes<HTMLDivElement> { children?: React.ReactNode; sidebar?: React.ReactNode; header?: React.ReactNode; statusStrip?: React.ReactNode }
export declare function AppShell(props: AppShellProps): React.ReactNode;
export interface SidebarProps extends React.HTMLAttributes<HTMLElement> { children?: React.ReactNode; brand?: React.ReactNode }
export declare function Sidebar(props: SidebarProps): React.ReactNode;
export interface SidebarSectionProps extends React.HTMLAttributes<HTMLElement> { children?: React.ReactNode; title?: React.ReactNode }
export declare function SidebarSection(props: SidebarSectionProps): React.ReactNode;
export interface SidebarItemProps extends React.AnchorHTMLAttributes<HTMLAnchorElement> { children?: React.ReactNode; href?: string; active?: boolean; glyph?: React.ReactNode }
export declare function SidebarItem(props: SidebarItemProps): React.ReactNode;
export interface NavigationItem { id: string; label: React.ReactNode; href: string; glyph?: React.ReactNode }
export interface PrimaryNavigationProps extends React.HTMLAttributes<HTMLDivElement> { primary: NavigationItem[]; secondary?: NavigationItem[]; activeId?: string }
export declare function PrimaryNavigation(props: PrimaryNavigationProps): React.ReactNode;
export interface TopHeaderProps extends React.HTMLAttributes<HTMLElement> { children?: React.ReactNode; title?: React.ReactNode; detail?: React.ReactNode; actions?: React.ReactNode }
export declare function TopHeader(props: TopHeaderProps): React.ReactNode;
export declare function StatusStrip(props: React.HTMLAttributes<HTMLDivElement> & { children?: React.ReactNode }): React.ReactNode;
export interface ActionableSystemNoticeProps extends React.HTMLAttributes<HTMLDivElement> { title: React.ReactNode; detail?: React.ReactNode; actionHref?: string; actionLabel?: React.ReactNode; kind?: "warning" | "error" }
export declare function ActionableSystemNotice(props: ActionableSystemNoticeProps): React.ReactNode;
export interface PageIntroProps extends React.HTMLAttributes<HTMLElement> { title: React.ReactNode; eyebrow?: React.ReactNode; actions?: React.ReactNode; children?: React.ReactNode }
export declare function PageIntro(props: PageIntroProps): React.ReactNode;
export declare function Panel(props: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }): React.ReactNode;
export interface PanelHeaderProps extends React.HTMLAttributes<HTMLElement> { children?: React.ReactNode; title?: React.ReactNode; meta?: React.ReactNode; actions?: React.ReactNode }
export declare function PanelHeader(props: PanelHeaderProps): React.ReactNode;
export interface TaskComposerShellProps extends React.HTMLAttributes<HTMLElement> { title?: React.ReactNode; meta?: React.ReactNode; children?: React.ReactNode }
export declare function TaskComposerShell(props: TaskComposerShellProps): React.ReactNode;
export interface ProgressStage { id?: string; label: React.ReactNode }
export interface ProgressStagesProps extends React.OlHTMLAttributes<HTMLOListElement> { stages: Array<ProgressStage | string>; current?: number }
export declare function ProgressStages(props: ProgressStagesProps): React.ReactNode;
export interface ResultVerdictProps extends React.HTMLAttributes<HTMLElement> { status?: string; label?: React.ReactNode; detail?: React.ReactNode }
export declare function ResultVerdict(props: ResultVerdictProps): React.ReactNode;
export interface EvidenceDisclosureProps extends React.DetailsHTMLAttributes<HTMLDetailsElement> { summary?: React.ReactNode; children?: React.ReactNode }
export declare function EvidenceDisclosure(props: EvidenceDisclosureProps): React.ReactNode;
export interface MetricCardProps extends React.HTMLAttributes<HTMLElement> { label: React.ReactNode; value?: React.ReactNode; detail?: React.ReactNode; sparkline?: React.ReactNode }
export declare function MetricCard(props: MetricCardProps): React.ReactNode;
export interface DataColumn<Row> { key: string; header: React.ReactNode; className?: string; render?: (row: Row, index: number) => React.ReactNode }
export interface DataTableProps<Row> extends React.HTMLAttributes<HTMLTableElement> { columns: DataColumn<Row>[]; rows: Row[]; caption?: string; getRowKey?: (row: Row, index: number) => string | number; empty?: React.ReactNode }
export declare function DataTable<Row>(props: DataTableProps<Row>): React.ReactNode;
export declare function EventTable<Row>(props: DataTableProps<Row>): React.ReactNode;
export interface StatusBadgeProps extends React.HTMLAttributes<HTMLSpanElement> { status?: string; label?: React.ReactNode }
export declare function StatusBadge(props: StatusBadgeProps): React.ReactNode;
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> { children?: React.ReactNode; variant?: string }
export declare function Button(props: ButtonProps): React.ReactNode;
export declare function PrimaryAction(props: ButtonProps): React.ReactNode;
export declare function SecondaryAction(props: ButtonProps): React.ReactNode;
export interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> { children?: React.ReactNode; label?: string }
export declare function IconButton(props: IconButtonProps): React.ReactNode;
export interface FormFieldProps extends React.HTMLAttributes<HTMLDivElement> { label?: React.ReactNode; id?: string; help?: React.ReactNode; error?: React.ReactNode; required?: boolean; children?: React.ReactNode }
export declare function FormField(props: FormFieldProps): React.ReactNode;
export interface TextInputProps extends React.InputHTMLAttributes<HTMLInputElement> { label?: React.ReactNode; help?: React.ReactNode; error?: React.ReactNode }
export declare function TextInput(props: TextInputProps): React.ReactNode;
export interface SelectOption { value: string; label: React.ReactNode; disabled?: boolean }
export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> { children?: React.ReactNode; label?: React.ReactNode; help?: React.ReactNode; error?: React.ReactNode; options?: SelectOption[] }
export declare function Select(props: SelectProps): React.ReactNode;
export interface TextAreaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> { label?: React.ReactNode; help?: React.ReactNode; error?: React.ReactNode }
export declare function TextArea(props: TextAreaProps): React.ReactNode;
export interface CostDisplayProps extends React.HTMLAttributes<HTMLSpanElement> { value: number | null; currency?: string | null; accountingStatus?: string | null }
export declare function CostDisplay(props: CostDisplayProps): React.ReactNode;
export interface DurationDisplayProps extends React.HTMLAttributes<HTMLSpanElement> { milliseconds: number | null }
export declare function DurationDisplay(props: DurationDisplayProps): React.ReactNode;
export interface TabDefinition { id: string; label: React.ReactNode; controls?: string; disabled?: boolean }
export interface TabsProps extends React.HTMLAttributes<HTMLDivElement> { tabs: TabDefinition[]; activeId?: string; onChange?: (id: string) => void; label?: string }
export declare function Tabs(props: TabsProps): React.ReactNode;
export interface TooltipProps extends React.HTMLAttributes<HTMLSpanElement> { children?: React.ReactNode; content: React.ReactNode }
export declare function Tooltip(props: TooltipProps): React.ReactNode;
export interface OverlayProps extends React.HTMLAttributes<HTMLElement> { children?: React.ReactNode; open: boolean; title: string; onClose?: () => void }
export declare function Dialog(props: OverlayProps): React.ReactNode;
export declare function Drawer(props: OverlayProps): React.ReactNode;
export interface StateProps extends React.HTMLAttributes<HTMLDivElement> { children?: React.ReactNode; title?: string; detail?: React.ReactNode }
export declare function EmptyState(props: StateProps): React.ReactNode;
export declare function ErrorState(props: StateProps): React.ReactNode;
export declare function LoadingState(props: StateProps): React.ReactNode;
export declare function Timeline(props: React.OlHTMLAttributes<HTMLOListElement> & { children?: React.ReactNode }): React.ReactNode;
export interface TimelineNodeProps extends React.LiHTMLAttributes<HTMLLIElement> { children?: React.ReactNode; title: React.ReactNode; meta?: React.ReactNode; marker?: React.ReactNode; active?: boolean }
export declare function TimelineNode(props: TimelineNodeProps): React.ReactNode;
export type KeyValueItem = readonly [React.ReactNode, React.ReactNode] | { label: React.ReactNode; value: React.ReactNode };
export interface KeyValueGridProps extends React.HTMLAttributes<HTMLDListElement> { items: KeyValueItem[] }
export declare function KeyValueGrid(props: KeyValueGridProps): React.ReactNode;
export declare function AsciiCorners(): React.ReactNode;
export declare function AsciiFrame(props: React.HTMLAttributes<HTMLDivElement> & { children?: React.ReactNode }): React.ReactNode;
export interface SparklineProps extends React.SVGAttributes<SVGSVGElement> { values?: number[]; label?: string }
export declare function Sparkline(props: SparklineProps): React.ReactNode;
