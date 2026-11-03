import { ReactNode, isValidElement, cloneElement, useId, useRef } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { ChevronDownIcon, Cross2Icon } from "@radix-ui/react-icons";
export function Button({
  children,
  onClick,
  kind = "",
  disabled = false,
  type = "button",
  ...props
}: {
  children: ReactNode;
  onClick?: () => void;
  kind?: string;
  disabled?: boolean;
  type?: "button" | "submit";
  [key: string]: any;
}) {
  return (
    <button
      type={type}
      className={"button " + kind}
      onClick={onClick}
      disabled={disabled}
      {...props}
    >
      {children}
    </button>
  );
}
export function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={"badge " + tone}>{children}</span>;
}
export function Panel({
  title,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={"panel " + className}>
      {title && (
        <div className="panel-head">
          <h2>{title}</h2>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
export function Field({
  label,
  children,
  hint,
  compact = false,
}: {
  label: string;
  children: ReactNode;
  hint?: ReactNode;
  compact?: boolean;
}) {
  const id = useId();
  const control =
    isValidElement<any>(children) && typeof children.type === "string"
      ? cloneElement(children, {
          "aria-label": label,
          "aria-describedby": hint ? id : undefined,
        })
      : children;
  return (
    <label className={"field" + (compact ? " field-compact" : "")}>
      <span className={compact ? "sr-only" : undefined}>{label}</span>
      {isValidElement(children) && children.type === "select" ? (
        <span className="select-control">
          {control}
          <ChevronDownIcon className="select-chevron" aria-hidden="true" />
        </span>
      ) : (
        control
      )}
      {hint && <small id={id}>{hint}</small>}
    </label>
  );
}
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  description: string;
  children: ReactNode;
}) {
  const returnFocus = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="overlay" />
        <Dialog.Content
          className="modal"
          onOpenAutoFocus={() => {
            returnFocus.current =
              document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
          }}
          onCloseAutoFocus={(event) => {
            if (returnFocus.current?.isConnected) {
              event.preventDefault();
              returnFocus.current.focus();
            }
          }}
        >
          <Dialog.Title>{title}</Dialog.Title>
          <Dialog.Description>{description}</Dialog.Description>
          <Dialog.Close className="icon-button close" aria-label="Close">
            <Cross2Icon />
          </Dialog.Close>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
