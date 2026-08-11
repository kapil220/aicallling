"use client";

import { AlertTriangle, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
    getWorkflowsSummaryApiV1WorkflowSummaryGet,
    listNumbersApiV1PlatformNumbersGet,
    releaseNumberApiV1PlatformNumbersPhoneNumberIdReleasePost,
    setInboundWorkflowApiV1PlatformNumbersPhoneNumberIdInboundWorkflowPut,
} from "@/client/sdk.gen";
import type {
    PlatformNumberResponse,
    WorkflowSummaryResponse,
} from "@/client/types.gen";
import { GetNumberDialog } from "@/components/telephony/GetNumberDialog";
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { useAppConfig } from "@/context/AppConfigContext";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

const NO_WORKFLOW = "__none__";

const STATUS_VARIANT: Record<string, "default" | "destructive" | "secondary"> = {
    active: "default",
    past_due: "destructive",
    released: "secondary",
};

export default function PhoneNumbersPage() {
    const { config, loading: configLoading } = useAppConfig();
    const { user, loading: authLoading } = useAuth();
    const router = useRouter();
    const [numbers, setNumbers] = useState<PlatformNumberResponse[]>([]);
    const [workflows, setWorkflows] = useState<WorkflowSummaryResponse[]>([]);
    const [loading, setLoading] = useState(true);
    const [dialogOpen, setDialogOpen] = useState(false);
    const [releaseTarget, setReleaseTarget] = useState<PlatformNumberResponse | null>(null);
    const hasFetched = useRef(false);

    const fetchNumbers = useCallback(async () => {
        const response = await listNumbersApiV1PlatformNumbersGet();
        if (!response.error && response.data) setNumbers(response.data);
        setLoading(false);
    }, []);

    useEffect(() => {
        if (configLoading || authLoading || !user || hasFetched.current) return;
        if (config?.deploymentMode !== "saas") {
            router.replace("/");
            return;
        }
        hasFetched.current = true;
        fetchNumbers();
        getWorkflowsSummaryApiV1WorkflowSummaryGet({
            query: { status: "active" },
        }).then((response) => {
            if (!response.error && response.data) setWorkflows(response.data);
        });
    }, [configLoading, authLoading, user, config?.deploymentMode, router, fetchNumbers]);

    const handleInboundChange = async (
        number: PlatformNumberResponse,
        value: string,
    ) => {
        const response = await setInboundWorkflowApiV1PlatformNumbersPhoneNumberIdInboundWorkflowPut({
            path: { phone_number_id: number.id },
            body: {
                inbound_workflow_id: value === NO_WORKFLOW ? null : Number(value),
            },
        });
        if (response.error) {
            toast.error(detailFromError(response.error, "Could not update inbound agent"));
            return;
        }
        toast.success("Inbound agent updated");
        fetchNumbers();
    };

    const handleRelease = async () => {
        if (!releaseTarget) return;
        const response = await releaseNumberApiV1PlatformNumbersPhoneNumberIdReleasePost({
            path: { phone_number_id: releaseTarget.id },
        });
        setReleaseTarget(null);
        if (response.error) {
            toast.error(detailFromError(response.error, "Could not release the number"));
            return;
        }
        toast.success("Number released");
        fetchNumbers();
    };

    const hasPastDue = numbers.some((n) => n.rental_status === "past_due");

    if (loading) {
        return (
            <div className="container mx-auto p-6 space-y-6">
                <Skeleton className="h-9 w-56" />
                <Skeleton className="h-80 rounded-lg" />
            </div>
        );
    }

    return (
        <div className="container mx-auto p-6 space-y-6">
            <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
                <div>
                    <h1 className="text-3xl font-bold mb-2">Phone Numbers</h1>
                    <p className="text-muted-foreground">
                        Platform numbers rented from your minutes balance. Inbound calls
                        route to the agent you pick.
                    </p>
                </div>
                <Button onClick={() => setDialogOpen(true)}>
                    <Plus className="h-4 w-4 mr-2" />
                    Get a number
                </Button>
            </div>

            {hasPastDue && (
                <div className="flex gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-900/50 dark:bg-amber-950/30">
                    <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-600 dark:text-amber-400" />
                    <div className="text-sm text-amber-900 dark:text-amber-200">
                        <p className="font-medium">A number rental is past due</p>
                        <p className="mt-1">
                            Top up your minutes on the Billing page, or the number will be
                            released after the grace period.
                        </p>
                    </div>
                </div>
            )}

            {numbers.length === 0 ? (
                <div className="rounded-lg border border-dashed p-12 text-center text-muted-foreground">
                    No platform numbers yet. Get one to receive and place calls without
                    bringing your own telephony account.
                </div>
            ) : (
                <div className="bg-card border rounded-lg overflow-x-auto shadow-sm">
                    <Table>
                        <TableHeader>
                            <TableRow className="bg-muted/50">
                                <TableHead>Number</TableHead>
                                <TableHead>Country</TableHead>
                                <TableHead>Status</TableHead>
                                <TableHead>Renews</TableHead>
                                <TableHead>Rental</TableHead>
                                <TableHead>Inbound agent</TableHead>
                                <TableHead className="text-right">Actions</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {numbers.map((number) => (
                                <TableRow key={number.id}>
                                    <TableCell className="font-medium">{number.address}</TableCell>
                                    <TableCell>{number.country_code ?? "-"}</TableCell>
                                    <TableCell>
                                        <Badge
                                            variant={
                                                STATUS_VARIANT[number.rental_status ?? ""] ??
                                                "secondary"
                                            }
                                        >
                                            {number.rental_status ?? "unknown"}
                                        </Badge>
                                    </TableCell>
                                    <TableCell>
                                        {number.next_renewal_at
                                            ? new Date(number.next_renewal_at).toLocaleDateString()
                                            : "-"}
                                    </TableCell>
                                    <TableCell>
                                        {number.rental_minutes_equivalent != null
                                            ? `${number.rental_minutes_equivalent} min/mo`
                                            : "-"}
                                    </TableCell>
                                    <TableCell>
                                        {number.rental_status === "released" ? (
                                            "-"
                                        ) : (
                                            <Select
                                                value={
                                                    number.inbound_workflow_id != null
                                                        ? String(number.inbound_workflow_id)
                                                        : NO_WORKFLOW
                                                }
                                                onValueChange={(value) =>
                                                    handleInboundChange(number, value)
                                                }
                                            >
                                                <SelectTrigger className="w-44">
                                                    <SelectValue />
                                                </SelectTrigger>
                                                <SelectContent>
                                                    <SelectItem value={NO_WORKFLOW}>(none)</SelectItem>
                                                    {workflows.map((w) => (
                                                        <SelectItem key={w.id} value={String(w.id)}>
                                                            #{w.id} - {w.name}
                                                        </SelectItem>
                                                    ))}
                                                </SelectContent>
                                            </Select>
                                        )}
                                    </TableCell>
                                    <TableCell className="text-right">
                                        {number.rental_status !== "released" && (
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                onClick={() => setReleaseTarget(number)}
                                            >
                                                Release
                                            </Button>
                                        )}
                                    </TableCell>
                                </TableRow>
                            ))}
                        </TableBody>
                    </Table>
                </div>
            )}

            <GetNumberDialog
                open={dialogOpen}
                onOpenChange={setDialogOpen}
                onPurchased={fetchNumbers}
            />

            <AlertDialog
                open={releaseTarget !== null}
                onOpenChange={(open) => !open && setReleaseTarget(null)}
            >
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>
                            Release {releaseTarget?.address}?
                        </AlertDialogTitle>
                        <AlertDialogDescription>
                            The number is returned to the carrier immediately and cannot be
                            recovered. Inbound calls to it will stop working.
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction onClick={handleRelease}>
                            Release number
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
}
