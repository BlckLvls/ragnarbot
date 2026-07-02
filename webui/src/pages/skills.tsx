import { Page } from '../app/shell'
import { EmptyState } from '../components/ui'

export default function Placeholder() {
  return (
    <Page title="skills">
      <div className="p-6"><EmptyState title="Coming right up" /></div>
    </Page>
  )
}
