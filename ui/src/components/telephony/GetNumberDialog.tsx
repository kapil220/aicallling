"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import {
    getPricingApiV1PlatformNumbersPricingGet,
    getWorkflowsSummaryApiV1WorkflowSummaryGet,
    purchaseNumberApiV1PlatformNumbersPurchasePost,
    searchNumbersApiV1PlatformNumbersSearchGet,
} from "@/client/sdk.gen";
import type {
    AvailableNumberResponse,
    PricingResponse,
    WorkflowSummaryResponse,
} from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { detailFromError } from "@/lib/apiError";

const NO_WORKFLOW = "__none__";

interface GetNumberDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onPurchased: () => void;
}

export function GetNumberDialog({ open, onOpenChange, onPurchased }: GetNumberDialogProps) {
    const [pricing, setPricing] = useState<PricingResponse | null>(null);
    const [workflows, setWorkflows] = useState<WorkflowSummaryResponse[]>([]);
    const [country, setCountry] = useState("US");
    const [areaCode, setAreaCode] = useState("");
    const [results, setResults] = useState<AvailableNumberResponse[] | null>(null);
    const [searching, setSearching] = useState(false);
    const [busyNumber, setBusyNumber] = useState<string | null>(null);
    const [inboundWorkflowId, setInboundWorkflowId] = useState<string>(NO_WORKFLOW);

    useEffect(() => {
        if (!open) return;
        getPricingApiV1PlatformNumbersPricingGet().then((response) => {
            if (!response.error && response.data) setPricing(response.data);
        });
        getWorkflowsSummaryApiV1WorkflowSummaryGet({
            query: { status: "active" },
        }).then((response) => {
            if (!response.error && response.data) setWorkflows(response.data);
        });
    }, [open]);

    const selectedPricing = pricing?.countries.find((c) => c.country === country);

    const handleSearch = async () => {
        setSearching(true);
        const response = await searchNumbersApiV1PlatformNumbersSearchGet({
            query: { country, area_code: areaCode || undefined },
        });
        setSearching(false);
        if (response.error || !response.data) {
            toast.error(detailFromError(response.error, "Number search failed"));
            return;
        }
        setResults(response.data);
    };

    const handleBuy = async (phoneNumber: string) => {
        setBusyNumber(phoneNumber);
        const response = await purchaseNumberApiV1PlatformNumbersPurchasePost({
            body: {
                phone_number: phoneNumber,
                country,
                inbound_workflow_id:
                    inboundWorkflowId === NO_WORKFLOW ? null : Number(inboundWorkflowId),
            },
        });
        setBusyNumber(null);
        if (response.error || !response.data) {
            toast.error(detailFromError(response.error, "Could not purchase the number"));
            return;
        }
        toast.success(`Purchased ${response.data.address}`);
        setResults(null);
        onOpenChange(false);
        onPurchased();
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
                <DialogHeader>
                    <DialogTitle>Get a number</DialogTitle>
                    <DialogDescription>
                        Search and buy a platform number. The monthly rental is deducted
                        from your minutes balance
                        {selectedPricing
                            ? ` (${selectedPricing.rental_minutes} min/mo for ${country})`
                            : ""}
                        , plus {pricing ? pricing.carrier_cents_per_minute / 100 : "-"}{" "}
                        extra min per calling minute.
                    </DialogDescription>
                </DialogHeader>

                <div className="grid gap-4">
                    <div className="grid grid-cols-2 gap-3">
                        <div className="grid gap-1.5">
                            <Label>Country</Label>
                            <Select value={country} onValueChange={setCountry}>
                                <SelectTrigger>
                                    <SelectValue placeholder="Country" />
                                </SelectTrigger>
                                <SelectContent>
                                    {(pricing?.countries ?? []).map((c) => (
                                        <SelectItem key={c.country} value={c.country}>
                                            {c.country} — {c.rental_minutes} min/mo
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="grid gap-1.5">
                            <Label>Area code (optional)</Label>
                            <Input
                                value={areaCode}
                                onChange={(e) => setAreaCode(e.target.value)}
                                placeholder="e.g. 415"
                            />
                        </div>
                    </div>

                    <div className="grid gap-1.5">
                        <Label>Inbound agent (optional)</Label>
                        <Select value={inboundWorkflowId} onValueChange={setInboundWorkflowId}>
                            <SelectTrigger>
                                <SelectValue placeholder="(none)" />
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
                    </div>

                    {results !== null && (
                        <div className="grid gap-2">
                            {results.length === 0 && (
                                <p className="text-sm text-muted-foreground">
                                    No numbers found — try another area code.
                                </p>
                            )}
                            {results.map((result) => (
                                <div
                                    key={result.phone_number}
                                    className="flex items-center justify-between rounded-lg border p-3"
                                >
                                    <div>
                                        <div className="font-medium">{result.phone_number}</div>
                                        <div className="text-xs text-muted-foreground">
                                            {[result.locality, result.region]
                                                .filter(Boolean)
                                                .join(", ") || "—"}{" "}
                                            · {result.rental_cents / 100} min/mo
                                        </div>
                                    </div>
                                    <Button
                                        size="sm"
                                        disabled={busyNumber !== null}
                                        onClick={() => handleBuy(result.phone_number)}
                                    >
                                        {busyNumber === result.phone_number ? "Buying..." : "Buy"}
                                    </Button>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                <DialogFooter>
                    <Button variant="outline" onClick={() => onOpenChange(false)}>
                        Cancel
                    </Button>
                    <Button onClick={handleSearch} disabled={searching}>
                        {searching ? "Searching..." : "Search numbers"}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
