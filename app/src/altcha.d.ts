// The `altcha` package registers an <altcha-widget> custom element.
import type { DetailedHTMLProps, HTMLAttributes } from "react";

declare global {
  namespace JSX {
    interface IntrinsicElements {
      "altcha-widget": DetailedHTMLProps<
        HTMLAttributes<HTMLElement> & {
          challengejson?: string;
          strings?: string;
          hidefooter?: boolean | string;
          hidelogo?: boolean | string;
          auto?: string;
        },
        HTMLElement
      >;
    }
  }
}

export {};
