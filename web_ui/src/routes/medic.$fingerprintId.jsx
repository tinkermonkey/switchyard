import { createFileRoute } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'
import { Link } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import FailureSignatureDetail from '../components/FailureSignatureDetail'

export const Route = createFileRoute('/medic/$fingerprintId')({
  component: MedicDetail,
})

function MedicDetail() {
  const { fingerprintId } = Route.useParams()

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />

      <div className="mt-6">
        {/* Back Button */}
        <Link
          to="/medic"
          className="inline-flex items-center gap-2 px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm mb-4"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to Medic
        </Link>

        {/* Detail View */}
        <FailureSignatureDetail fingerprintId={fingerprintId} />
      </div>
    </div>
  )
}
